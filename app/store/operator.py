"""Operator realm credentials — encrypted-at-rest keys, separate from user realm."""
import json
import logging
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from app.core.crypto import generate_operator_key, hash_key, prefix_of
from app.core.ids import new_id
from app.security.secrets import get_secrets_manager

logger = logging.getLogger("palimpsest.operator")


# KEYS[1] = record, KEYS[2] = id set; ARGV[1] = record-key prefix.
# Returns 1 revoked, 0 unknown, -1 refused (would leave no active key).
_REVOKE_LUA = """
if redis.call('EXISTS', KEYS[1]) == 0 then
  return 0
end
if redis.call('HGET', KEYS[1], 'status') == 'active' then
  local active = 0
  for _, id in ipairs(redis.call('SMEMBERS', KEYS[2])) do
    if redis.call('HGET', ARGV[1] .. id, 'status') == 'active' then
      active = active + 1
    end
  end
  if active <= 1 then
    return -1
  end
end
redis.call('HSET', KEYS[1], 'status', 'revoked')
return 1
"""

# Update last_used_at only on a record that still exists (never resurrect one).
_TOUCH_LUA = """
if redis.call('EXISTS', KEYS[1]) == 1 then
  redis.call('HSET', KEYS[1], 'last_used_at', ARGV[1])
end
return 0
"""


class LastActiveKeyError(Exception):
    """Revoking this key would leave no active operator key."""


class OperatorStore:
    """Operator-realm key CRUD under the ``pgw:operator:*`` namespace."""

    def __init__(self, redis: Redis):
        """Initialize.

        Args:
            redis: Shared async Redis client
        """
        self.redis = redis
        self.secrets = get_secrets_manager()

    # Key builders (operator realm only)

    def _key_record_key(self, key_id: str) -> str:
        """Operator key record (encrypted)."""
        return f"pgw:operator:api_key:{key_id}"

    def _key_hash_index_key(self, key_hash: str) -> str:
        """Operator key hash index (O(1) lookup)."""
        return f"pgw:operator:api_key:by_hash:{key_hash}"

    def _keys_set_key(self) -> str:
        """Operator keys set (for listing)."""
        return "pgw:operator:api_keys"

    # CRUD

    async def _store_key_record(self, raw_key: str) -> dict[str, Any]:
        """Write the record + hash index + listing entry for ``raw_key`` atomically.

        A single Redis pipeline (transaction) keeps the three writes from ever
        being observed half-applied — e.g. indexed-but-unlisted after a crash
        between calls, which would produce a credential that authenticates but
        can never be found (or revoked) via ``list_keys``.
        """
        key_id = new_id()
        key_hash = hash_key(raw_key)
        now = datetime.now(UTC).isoformat()
        prefix = prefix_of(raw_key)
        ciphertext = self.secrets.encrypt(raw_key)

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hset(
                self._key_record_key(key_id),
                mapping={
                    "id": key_id,
                    "prefix": prefix,
                    "key_material": ciphertext,
                    "created_at": now,
                    "last_used_at": now,
                    "status": "active",
                },
            )
            pipe.set(self._key_hash_index_key(key_hash), key_id)
            pipe.sadd(self._keys_set_key(), key_id)
            await pipe.execute()

        return {"id": key_id, "prefix": prefix, "created_at": now}

    async def create_key(self) -> dict[str, Any]:
        """Create new operator key (raw, encrypted, indexed).

        Returns:
            {id, prefix, key (raw), created_at}
        """
        raw_key = generate_operator_key()  # pgw_opk_... format
        record = await self._store_key_record(raw_key)
        return {**record, "key": raw_key}

    async def active_key_count(self) -> int:
        """Number of operator keys that can still authenticate."""
        count = 0
        for key_id in await self.redis.smembers(self._keys_set_key()):
            if isinstance(key_id, bytes):
                key_id = key_id.decode()
            status = await self.redis.hget(self._key_record_key(key_id), "status")
            if isinstance(status, bytes):
                status = status.decode()
            if status == "active":
                count += 1
        return count

    async def has_any_key(self) -> bool:
        """True if at least one operator key is registered."""
        return await self.redis.scard(self._keys_set_key()) > 0

    async def seed_bootstrap_key(self, raw_key: str) -> None:
        """Idempotently seed the first operator key from env.

        No-op while at least one *active* operator key exists; the raw env value
        is never persisted, only its Fernet ciphertext and SHA-256 index. When
        every key has been revoked this restores access (recovery path): the
        env key's own revoked record is reactivated, otherwise it is stored anew.
        """
        if await self.active_key_count() > 0:
            return
        existing = await self.get_by_hash(hash_key(raw_key))
        if existing is not None:
            logger.warning(
                json.dumps({"event": "operator_bootstrap_key_reactivated", "key_id": existing["id"]})
            )
            await self.redis.hset(self._key_record_key(existing["id"]), "status", "active")
            return
        await self._store_key_record(raw_key)

    async def get_by_hash(self, key_hash: str) -> dict[str, Any] | None:
        """Get operator key by SHA-256 hash (edge auth path).

        Args:
            key_hash: SHA-256 hash of raw key (hex)

        Returns:
            Record dict or None
        """
        key_id = await self.redis.get(self._key_hash_index_key(key_hash))
        if not key_id:
            return None

        if isinstance(key_id, bytes):
            key_id = key_id.decode()

        record = await self.redis.hgetall(self._key_record_key(key_id))
        return self._decode_dict(record) if record else None

    async def list_keys(self) -> list[dict[str, Any]]:
        """List all operator keys (no key_material/key_hash).

        Returns:
            List of {id, prefix, created_at, last_used_at, status}
        """
        key_ids = await self.redis.smembers(self._keys_set_key())
        keys = []

        for key_id in key_ids:
            if isinstance(key_id, bytes):
                key_id = key_id.decode()

            record = await self.redis.hgetall(self._key_record_key(key_id))
            decoded = self._decode_dict(record)

            if decoded:
                try:
                    keys.append({
                        "id": decoded["id"],
                        "prefix": decoded["prefix"],
                        "created_at": decoded["created_at"],
                        "last_used_at": decoded.get("last_used_at", ""),
                        "status": decoded["status"],
                    })
                except (KeyError, TypeError):
                    logger.warning(
                        json.dumps({"event": "operator_key_corrupt", "key_id": key_id})
                    )
                    continue

        return keys

    async def revoke_key(self, key_id: str) -> bool:
        """Revoke operator key immediately.

        The last-active-key check and the write are one Lua script, so two
        concurrent revocations cannot lock every operator out.

        Args:
            key_id: Key ID

        Returns:
            True if revoked, False if not found

        Raises:
            LastActiveKeyError: ``key_id`` is the only active key, so revoking
                it would lock every operator out.
        """
        result = int(
            await self.redis.eval(
                _REVOKE_LUA,
                2,
                self._key_record_key(key_id),
                self._keys_set_key(),
                self._key_record_key(""),
            )
        )
        if result == -1:
            raise LastActiveKeyError(key_id)
        return result == 1

    async def touch_last_used(self, key_id: str) -> None:
        """Record that ``key_id`` just authenticated (best-effort, never raises)."""
        try:
            await self.redis.eval(
                _TOUCH_LUA, 1, self._key_record_key(key_id), datetime.now(UTC).isoformat()
            )
        except Exception:
            logger.debug("operator last_used_at update failed", exc_info=True)

    async def get_decrypted_key(self, key_id: str) -> str | None:
        """Decrypt and return raw key (internal auth only).

        Args:
            key_id: Key ID

        Returns:
            Decrypted raw key, or None if not found, revoked, or undecryptable
        """
        record = await self.redis.hgetall(self._key_record_key(key_id))
        if not record:
            return None

        decoded = self._decode_dict(record)
        if decoded.get("status") != "active":
            return None

        ciphertext = decoded.get("key_material")

        if not ciphertext:
            return None

        try:
            return self.secrets.decrypt(ciphertext)
        except ValueError:
            return None

    def _decode_dict(self, data: dict[Any, Any]) -> dict[str, Any]:
        """Decode Redis response (handles bytes)."""
        if not data:
            return {}
        return {
            (k.decode() if isinstance(k, bytes) else k): (
                v.decode() if isinstance(v, bytes) else v
            )
            for k, v in data.items()
        }
