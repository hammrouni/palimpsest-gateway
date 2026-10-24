"""Provider Key Pool registry: key CRUD, key state, routable reads, decryption.

The only writer of ``pgw:pool:keys*`` and ``pgw:pool:state:*``. Settlement owns
the per-key used counters; ``app.pool.events`` owns the failover-event list.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from redis.asyncio import Redis

from app.config import get_settings
from app.core.ids import new_id
from app.pool.limits import clear_observed
from app.pool.models import (
    KeyCreate,
    KeyRecord,
    KeyState,
    KeyStateView,
    KeyUpdate,
    Provider,
)
from app.pool.tokens import carry_over, counter_pattern
from app.security.secrets import get_secrets_manager

_KEYS_SET = "pgw:pool:keys"

# Key ids are generated UUIDs; anything else could address another key type.
_KEY_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")

# HSET only if the hash still exists, so a write racing remove() can never
# resurrect a deleted record as a partial hash.
_CONDITIONAL_HSET_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('HSET', KEYS[1], unpack(ARGV))
  return 1
end
return 0
"""


def _record_key(key_id: str) -> str:
    return f"pgw:pool:keys:{key_id}"


def _digest_key(key_digest: str) -> str:
    return f"pgw:pool:keys:by_digest:{key_digest}"


def _state_key(key_id: str) -> str:
    return f"pgw:pool:state:{key_id}"


def _model_state_key(key_id: str, model: str) -> str:
    """Cooldown marker for one model on one key (the key stays routable for others)."""
    return f"{_state_key(key_id)}:model:{model}"


def _decode_dict(data: dict[Any, Any]) -> dict[str, Any]:
    """Decode a Redis hash response (handles bytes from fakeredis)."""
    if not data:
        return {}
    return {
        (k.decode() if isinstance(k, bytes) else k): (
            v.decode() if isinstance(v, bytes) else v
        )
        for k, v in data.items()
    }


def _decode_members(members: Any) -> list[str]:
    return [m.decode() if isinstance(m, bytes) else m for m in members]


def _as_aware_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _next_utc_midnight(now: datetime) -> datetime:
    return datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=UTC)


# A provider's daily reset zone when it is not UTC. Empty: observed provider
# 429s count down to midnight UTC.
_RESET_ZONE: dict[Provider, str] = {}


def _next_midnight(now: datetime, zone_name: str | None) -> datetime:
    """The next midnight in ``zone_name`` (UTC if unknown or unavailable), as UTC."""
    if zone_name:
        try:
            zone = ZoneInfo(zone_name)
        except ZoneInfoNotFoundError:
            zone = None
        if zone is not None:
            local = now.astimezone(zone)
            tomorrow = (local + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0
            )
            return tomorrow.astimezone(UTC)
    return _next_utc_midnight(now)


def _key_hint(raw_key: str) -> str:
    """Operator-facing hint that never reveals the whole key.

    Short keys show only their first character.
    """
    if len(raw_key) <= 7:
        return f"{raw_key[:1]}…"
    return f"{raw_key[:3]}…{raw_key[-4:]}"


class KeyNotFoundError(Exception):
    """Raised when a key_id has no registry record (removed or never existed)."""


class DuplicateKeyError(Exception):
    """Raised on add when the same key material is already in the pool."""


def _require_valid_id(key_id: str) -> None:
    if not isinstance(key_id, str) or not _KEY_ID.fullmatch(key_id):
        raise KeyNotFoundError(key_id)


class PoolRegistry:
    """CRUD, state changes and reads for the Provider Key Pool."""

    def __init__(self, redis: Redis):
        self.redis = redis
        self.secrets = get_secrets_manager()

    # -- CRUD ------------------------------------------------------------

    async def create(self, data: KeyCreate, operator_id: str | None = None) -> KeyRecord:
        """Add a Provider Key. Raises ``DuplicateKeyError`` for repeated material.

        The digest slot is claimed with ``SET NX`` before the record is written,
        so concurrent adds of the same key cannot both commit.
        """
        digest = hashlib.sha256(data.key.encode()).hexdigest()

        key_id = new_id()
        now = datetime.now(UTC)
        reset_at = (
            _as_aware_utc(data.reset_at)
            if data.reset_at
            else _next_midnight(now, _RESET_ZONE.get(data.provider))
        )

        claimed = await self.redis.set(_digest_key(digest), key_id, nx=True)
        if not claimed:
            raise DuplicateKeyError(digest)

        record = {
            "id": key_id,
            "provider": data.provider.value,
            "models": json.dumps(data.models),
            "key_enc": self.secrets.encrypt(data.key),
            "key_hint": _key_hint(data.key),
            "key_digest": digest,
            "reset_at": reset_at.isoformat(),
            "reset_period_hours": str(data.reset_period_hours),
            "suspended": "0",
            "daily_cap": "" if data.daily_cap is None else str(data.daily_cap),
            "token_cap": "" if data.token_cap is None else str(data.token_cap),
            "label": data.label or "",
            "operator_id": operator_id or "",
            "last_modified_by": operator_id or "",
            "created_at": now.isoformat(),
            "updated_at": now.isoformat(),
        }

        try:
            pipe = self.redis.pipeline(transaction=True)
            pipe.hset(_record_key(key_id), mapping=record)
            pipe.sadd(_KEYS_SET, key_id)
            await pipe.execute()
        except Exception:
            await self.redis.delete(_digest_key(digest))
            raise

        return self._to_key_record(record, KeyState.ACTIVE)

    async def get(self, key_id: str) -> KeyRecord | None:
        """One record with its effective state, or ``None`` if absent or corrupt."""
        try:
            _require_valid_id(key_id)
        except KeyNotFoundError:
            return None
        pipe = self.redis.pipeline(transaction=False)
        pipe.hgetall(_record_key(key_id))
        pipe.hgetall(_state_key(key_id))
        raw_record, raw_marker = await pipe.execute()
        decoded = _decode_dict(raw_record)
        if not decoded:
            return None
        try:
            state = self._resolve_state(decoded, _decode_dict(raw_marker))
            return self._to_key_record(decoded, state)
        except (KeyError, ValueError):
            return None  # corrupt or partial record

    async def list(self) -> list[KeyRecord]:
        """All records with effective state, in one pipelined batch.

        Corrupt or partial records are skipped.
        """
        key_ids = _decode_members(await self.redis.smembers(_KEYS_SET))
        if not key_ids:
            return []

        pipe = self.redis.pipeline(transaction=False)
        for key_id in key_ids:
            pipe.hgetall(_record_key(key_id))
            pipe.hgetall(_state_key(key_id))
        rows = await pipe.execute()

        records: list[KeyRecord] = []
        for i, _key_id in enumerate(key_ids):
            decoded = _decode_dict(rows[2 * i])
            if not decoded:
                continue  # stale index entry
            try:
                state = self._resolve_state(decoded, _decode_dict(rows[2 * i + 1]))
                records.append(self._to_key_record(decoded, state))
            except (KeyError, ValueError):
                continue
        return records

    async def update(
        self, key_id: str, data: KeyUpdate, operator_id: str | None = None
    ) -> KeyRecord:
        """Edit a key's settings in place; its state is untouched.

        Token usage carries over to the new window when the schedule changes.
        """
        record = await self._require_record(key_id)
        previous = self._to_key_record(dict(record), KeyState.ACTIVE)
        mapping = {
            "updated_at": datetime.now(UTC).isoformat(),
            "last_modified_by": operator_id or "",
        }
        if "models" in data.model_fields_set:
            mapping["models"] = json.dumps(data.models)
        if "daily_cap" in data.model_fields_set:
            mapping["daily_cap"] = "" if data.daily_cap is None else str(data.daily_cap)
        if "label" in data.model_fields_set:
            mapping["label"] = data.label or ""
        if "reset_at" in data.model_fields_set:
            mapping["reset_at"] = _as_aware_utc(data.reset_at).isoformat()
        if "reset_period_hours" in data.model_fields_set:
            mapping["reset_period_hours"] = str(data.reset_period_hours)
        if "token_cap" in data.model_fields_set:
            mapping["token_cap"] = "" if data.token_cap is None else str(data.token_cap)
        if not await self._conditional_hset(_record_key(key_id), mapping):
            raise KeyNotFoundError(key_id)
        record.update(mapping)
        state = await self.effective_state(key_id)
        updated = self._to_key_record(record, state)
        if "reset_at" in mapping or "reset_period_hours" in mapping:
            await carry_over(self.redis, previous, updated)
        return updated

    async def suspend(self, key_id: str, operator_id: str | None = None) -> KeyRecord:
        """Exclude the key from selection at once. Idempotent.

        Independent of any cooldown or exhaustion marker.
        """
        record = await self._require_record(key_id)
        now = datetime.now(UTC).isoformat()
        mapping = {"suspended": "1", "updated_at": now, "last_modified_by": operator_id or ""}
        if not await self._conditional_hset(_record_key(key_id), mapping):
            raise KeyNotFoundError(key_id)  # removed between read and write
        record.update(mapping)
        return self._to_key_record(record, KeyState.SUSPENDED)

    async def resume(self, key_id: str, operator_id: str | None = None) -> KeyRecord:
        """Clear the suspension flag only; a running exhaustion marker stays."""
        _require_valid_id(key_id)
        pipe = self.redis.pipeline(transaction=False)
        pipe.hgetall(_record_key(key_id))
        pipe.hgetall(_state_key(key_id))
        raw_record, raw_marker = await pipe.execute()
        decoded = _decode_dict(raw_record)
        if not decoded:
            raise KeyNotFoundError(key_id)
        now = datetime.now(UTC).isoformat()
        mapping = {"suspended": "0", "updated_at": now, "last_modified_by": operator_id or ""}
        if not await self._conditional_hset(_record_key(key_id), mapping):
            raise KeyNotFoundError(key_id)  # removed between read and write
        decoded.update(mapping)
        state = self._resolve_state(decoded, _decode_dict(raw_marker))
        return self._to_key_record(decoded, state)

    async def remove(self, key_id: str) -> None:
        """Delete the key with its index, digest, state markers and counters."""
        record = await self._require_record(key_id)
        pipe = self.redis.pipeline(transaction=True)
        pipe.delete(_record_key(key_id))
        pipe.srem(_KEYS_SET, key_id)
        digest = record.get("key_digest")
        if digest:
            pipe.delete(_digest_key(digest))
        pipe.delete(_state_key(key_id))
        await pipe.execute()
        async for marker in self.redis.scan_iter(match=_state_key(key_id) + ":model:*"):
            await self.redis.delete(marker)
        await clear_observed(self.redis, key_id)
        async for counter in self.redis.scan_iter(match=counter_pattern(key_id)):
            await self.redis.delete(counter)

    # -- Reads used by selection, failover and operator views ---------------

    async def routable_keys(self, model: str | None = None) -> list[KeyRecord]:
        """Active keys, optionally only those serving ``model``.

        With a ``model``, keys cooling down for that model are left out.
        """
        keys = await self.list()
        candidates = [
            k
            for k in keys
            if k.state == KeyState.ACTIVE and (model is None or model in k.models)
        ]
        if model is None or not candidates:
            return candidates
        pipe = self.redis.pipeline(transaction=False)
        for key in candidates:
            pipe.exists(_model_state_key(key.id, model))
        cooling = await pipe.execute()
        return [key for key, held in zip(candidates, cooling, strict=True) if not held]

    async def next_available_at(self, model: str) -> datetime | None:
        """When a key serving ``model`` is next expected to be routable again.

        The earliest end of a running cooldown or exhaustion; ``None`` if none
        is running (for example, every serving key is suspended).
        """
        now = datetime.now(UTC)
        best: datetime | None = None
        for record in await self.list():
            if model not in record.models or record.state == KeyState.SUSPENDED:
                continue
            marker = (
                _state_key(record.id)
                if record.state in (KeyState.EXHAUSTED, KeyState.COOLDOWN)
                else _model_state_key(record.id, model)
            )
            ttl = await self.redis.ttl(marker)
            if ttl is None or ttl <= 0:
                continue
            until = now + timedelta(seconds=ttl)
            if best is None or until < best:
                best = until
        return best

    async def _cooling_models(self, key_id: str) -> list[dict]:
        prefix = _state_key(key_id) + ":model:"
        cooling = []
        async for raw in self.redis.scan_iter(match=prefix + "*"):
            name = raw.decode() if isinstance(raw, bytes) else raw
            ttl = await self.redis.ttl(name)
            if ttl is not None and ttl > 0:
                cooling.append({"model": name[len(prefix):], "seconds_remaining": float(ttl)})
        return sorted(cooling, key=lambda item: item["model"])

    async def effective_state(self, key_id: str) -> KeyState:
        """The key's effective state; raises ``KeyNotFoundError`` if absent."""
        _require_valid_id(key_id)
        pipe = self.redis.pipeline(transaction=False)
        pipe.hgetall(_record_key(key_id))
        pipe.hgetall(_state_key(key_id))
        raw_record, raw_marker = await pipe.execute()
        decoded = _decode_dict(raw_record)
        if not decoded:
            raise KeyNotFoundError(key_id)
        return self._resolve_state(decoded, _decode_dict(raw_marker))

    async def get_key_material(self, key_id: str) -> str:
        """The decrypted key. Called only on the provider-call path.

        An undecryptable key (wrong or rotated master key) is reported as
        ``KeyNotFoundError``: the key is unusable either way.
        """
        _require_valid_id(key_id)
        decoded = _decode_dict(await self.redis.hgetall(_record_key(key_id)))
        if not decoded:
            raise KeyNotFoundError(key_id)
        try:
            return self.secrets.decrypt(decoded["key_enc"])
        except ValueError as exc:
            raise KeyNotFoundError(key_id) from exc

    async def mark_exhausted(self, key_id: str, seconds: float | None = None) -> None:
        """Exclude the whole key for ``seconds``, or until its next reset boundary if not given."""
        await self._mark(key_id, KeyState.EXHAUSTED, ttl_seconds=seconds)

    async def mark_cooldown(
        self, key_id: str, model: str | None = None, seconds: float | None = None
    ) -> None:
        """Exclude for ``seconds``, or ``cooldown_seconds`` when not given.

        With a ``model`` only that model is held back; otherwise the whole key.
        """
        await self._mark(
            key_id,
            KeyState.COOLDOWN,
            ttl_seconds=seconds or get_settings().cooldown_seconds,
            model=model,
        )

    async def list_states(self) -> list[KeyStateView]:
        """All keys + effective state + reset_at + time-remaining-on-marker."""
        views = []
        for record in await self.list():
            seconds_remaining = None
            if record.state in (KeyState.EXHAUSTED, KeyState.COOLDOWN):
                ttl = await self.redis.ttl(_state_key(record.id))
                if ttl is not None and ttl >= 0:
                    seconds_remaining = float(ttl)
            views.append(
                KeyStateView(
                    id=record.id,
                    provider=record.provider,
                    models=record.models,
                    state=record.state,
                    reset_at=record.reset_at,
                    seconds_remaining=seconds_remaining,
                    cooling_models=await self._cooling_models(record.id),
                )
            )
        return views

    # -- Internals ---------------------------------------------------------

    async def _require_record(self, key_id: str) -> dict[str, Any]:
        _require_valid_id(key_id)
        decoded = _decode_dict(await self.redis.hgetall(_record_key(key_id)))
        if not decoded:
            raise KeyNotFoundError(key_id)
        return decoded

    async def _conditional_hset(self, key: str, mapping: dict[str, str]) -> bool:
        """HSET ``mapping`` only if the hash still exists."""
        args: list[str] = []
        for field, value in mapping.items():
            args.extend((field, value))
        return int(await self.redis.eval(_CONDITIONAL_HSET_LUA, 1, key, *args)) == 1

    async def _mark(
        self,
        key_id: str,
        state: KeyState,
        ttl_seconds: float | None = None,
        model: str | None = None,
    ) -> None:
        record = await self._require_record(key_id)
        now = datetime.now(UTC)
        state_key = _state_key(key_id) if model is None else _model_state_key(key_id, model)

        if ttl_seconds is None:
            anchor = _as_aware_utc(datetime.fromisoformat(record["reset_at"]))
            period = timedelta(hours=int(record["reset_period_hours"]))
            if period.total_seconds() <= 0:
                period = timedelta(hours=24)
            k = max(0, math.ceil((now - anchor) / period))
            boundary = anchor + k * period
            ttl_seconds = (boundary - now).total_seconds()
            if ttl_seconds <= 0:
                ttl_seconds = period.total_seconds()

        new_until = now + timedelta(seconds=ttl_seconds)

        # Never shorten a live exclusion: a short cooldown must not replace an
        # exhaustion that lasts until the quota resets.
        marker = _decode_dict(await self.redis.hgetall(state_key))
        if marker.get("state") and marker.get("until"):
            try:
                existing_until = datetime.fromisoformat(marker["until"])  # type: ignore[arg-type]
            except ValueError:
                existing_until = None
            if existing_until is not None and existing_until > now and new_until < existing_until:
                return  # keep the longer exclusion intact

        since = now.isoformat()
        until = new_until.isoformat()

        pipe = self.redis.pipeline(transaction=True)
        pipe.hset(
            state_key,
            mapping={"state": state.value, "until": until, "since": since},
        )
        pipe.expire(state_key, max(1, math.ceil(ttl_seconds)))
        await pipe.execute()

    @staticmethod
    def _resolve_state(decoded: dict[str, Any], marker: dict[str, Any]) -> KeyState:
        """Suspended, else the live state marker, else active."""
        if decoded.get("suspended") == "1":
            return KeyState.SUSPENDED
        if marker.get("state"):
            return KeyState(marker["state"])
        return KeyState.ACTIVE

    def _to_key_record(self, decoded: dict[str, Any], state: KeyState) -> KeyRecord:
        daily_cap = decoded.get("daily_cap")
        return KeyRecord(
            id=decoded["id"],
            provider=decoded["provider"],
            models=json.loads(decoded["models"]),
            key_hint=decoded["key_hint"],
            reset_at=decoded["reset_at"],
            reset_period_hours=int(decoded["reset_period_hours"]),
            daily_cap=float(daily_cap) if daily_cap else None,
            token_cap=int(decoded["token_cap"]) if decoded.get("token_cap") else None,
            label=decoded.get("label") or None,
            state=state,
            operator_id=decoded.get("operator_id") or None,
            last_modified_by=decoded.get("last_modified_by") or None,
            created_at=decoded["created_at"],
            updated_at=decoded["updated_at"],
        )
