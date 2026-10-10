"""Session records and history. The only module that formats session keys."""
import json
import logging
from datetime import UTC, datetime
from typing import Any

from redis.asyncio import Redis

from app.core.ids import new_id

logger = logging.getLogger("palimpsest.session")

SESSION_TTL_SECONDS = 604800  # 7 days
SESSION_LOCK_TTL_SECONDS = 30

# Minimum gap between two stale-index sweeps of one account's session set.
CLEANUP_INTERVAL_SECONDS = 300

# Cap on stored exchanges per session, applied on write and on read.
MAX_HISTORY_EXCHANGES = 1000

# Idempotent claim: seed the full record only when the id field is new, so a
# reader never sees a record with only ``id`` set.
_CREATE_OR_CLAIM_SCRIPT = """
local created = redis.call('hsetnx', KEYS[1], 'id', ARGV[1])
if created == 1 then
    redis.call('hset', KEYS[1], 'created_at', ARGV[2], 'updated_at', ARGV[2], 'turn_count', '0', 'status', 'active')
end
return created
"""


class SessionNotFoundError(Exception):
    """A session write targeted a session that was never claimed."""


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


class SessionStore:
    """Owner-scoped session CRUD and history. Every method takes account_id first."""

    def __init__(self, redis: Redis, ttl_seconds: int = SESSION_TTL_SECONDS, max_exchanges: int = MAX_HISTORY_EXCHANGES):
        """Initialize.

        Args:
            redis: Async Redis client
            ttl_seconds: Session TTL in seconds
            max_exchanges: Cap on the history list length
        """
        if max_exchanges < 1:
            # ltrim(-0, -1) would keep the whole list.
            raise ValueError("max_exchanges must be >= 1")
        self.redis = redis
        self.ttl_seconds = ttl_seconds
        self.max_exchanges = max_exchanges
        self._create_or_claim_script = redis.register_script(_CREATE_OR_CLAIM_SCRIPT)

    # Key builders

    def _session_key(self, account_id: str, session_id: str) -> str:
        return f"pgw:user:{account_id}:session:{session_id}"

    def _history_key(self, account_id: str, session_id: str) -> str:
        return f"pgw:user:{account_id}:session:{session_id}:history"

    def _sessions_index_key(self, account_id: str) -> str:
        return f"pgw:user:{account_id}:sessions"

    def _lock_key(self, account_id: str, session_id: str) -> str:
        return f"pgw:user:{account_id}:session:{session_id}:lock"

    # Session CRUD

    async def create_or_claim(
        self, account_id: str, session_id: str | None = None
    ) -> dict[str, Any]:
        """Create or claim a session (idempotent).

        Args:
            account_id: Owner account
            session_id: Client-supplied ID, or None to generate one

        Returns:
            Session record {id, created_at, updated_at, turn_count, status}
        """
        if session_id is None:
            session_id = new_id()

        now = datetime.now(UTC).isoformat()
        key = self._session_key(account_id, session_id)

        created = await self._create_or_claim_script(
            keys=[key], args=[session_id, now]
        )

        index_key = self._sessions_index_key(account_id)
        async with self.redis.pipeline(transaction=True) as pipe:
            if created:
                pipe.sadd(index_key, session_id)
            pipe.expire(key, self.ttl_seconds)
            pipe.expire(self._history_key(account_id, session_id), self.ttl_seconds)
            pipe.expire(index_key, self.ttl_seconds)
            await pipe.execute()

        record = await self.redis.hgetall(key)
        return self._decode_dict(record) if record else {}

    async def get_metadata(self, account_id: str, session_id: str) -> dict[str, Any] | None:
        """Session metadata (no history), refreshing its TTL; None if absent."""
        key = self._session_key(account_id, session_id)
        record = await self.redis.hgetall(key)

        # Best effort: a failed sweep never blocks the read.
        try:
            await self._cleanup_stale_sessions(account_id)
        except Exception:
            logger.warning(
                json.dumps({"event": "stale_session_cleanup_failed", "account_id": account_id}),
                exc_info=True,
            )

        if not record:
            return None

        await self.redis.expire(key, self.ttl_seconds)
        await self.redis.expire(
            self._history_key(account_id, session_id), self.ttl_seconds
        )
        await self.redis.expire(
            self._sessions_index_key(account_id), self.ttl_seconds
        )

        decoded = self._decode_dict(record)
        try:
            turn_count = int(decoded.get("turn_count")) if decoded.get("turn_count") else 0
        except (TypeError, ValueError):
            logger.warning(
                json.dumps({"event": "turn_count_corrupt", "account_id": account_id, "session_id": session_id})
            )
            turn_count = 0
        return {
            "id": decoded.get("id"),
            "created_at": decoded.get("created_at"),
            "updated_at": decoded.get("updated_at"),
            "turn_count": turn_count,
        }

    async def append_exchange(
        self, account_id: str, session_id: str, messages: list[dict[str, Any]]
    ) -> None:
        """Append one exchange to session history.

        Raises:
            SessionNotFoundError: the session was never claimed for this account,
                so no partial record is created.
        """
        key = self._session_key(account_id, session_id)

        if not await self.redis.exists(key):
            raise SessionNotFoundError(
                f"session {session_id!r} not claimed for this account"
            )

        now = datetime.now(UTC).isoformat()
        exchange = {
            "ts": now,
            "messages": messages,
        }

        hist_key = self._history_key(account_id, session_id)

        # One transaction keeps turn_count in step with the list, and the trim
        # evicts the oldest entries beyond ``max_exchanges``.
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.rpush(hist_key, json.dumps(exchange))
            pipe.ltrim(hist_key, -self.max_exchanges, -1)
            pipe.hincrby(key, "turn_count", 1)
            pipe.hset(key, "updated_at", now)
            pipe.expire(key, self.ttl_seconds)
            pipe.expire(hist_key, self.ttl_seconds)
            pipe.expire(self._sessions_index_key(account_id), self.ttl_seconds)
            await pipe.execute()

    async def add_usage(
        self,
        account_id: str,
        session_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        model: str,
    ) -> None:
        """Add one turn's provider-reported tokens to the session record.

        Skipped when the session no longer exists, so no partial record is created.
        """
        key = self._session_key(account_id, session_id)
        if not await self.redis.exists(key):
            return
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.hincrby(key, "prompt_tokens", prompt_tokens)
            pipe.hincrby(key, "completion_tokens", completion_tokens)
            pipe.hset(key, "last_model", model[:64])
            await pipe.execute()

    async def read_recent(
        self, account_id: str, session_id: str, count: int = 10
    ) -> list[dict[str, Any]]:
        """The last ``count`` exchanges, oldest first, refreshing the TTL.

        ``count`` is capped at ``max_exchanges``; corrupt entries are skipped
        with a warning.

        Returns:
            List of exchange dicts [{ts, messages}, ...]
        """
        if count <= 0:
            return []
        count = min(count, self.max_exchanges)

        hist_key = self._history_key(account_id, session_id)
        key = self._session_key(account_id, session_id)

        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.expire(key, self.ttl_seconds)
            pipe.expire(hist_key, self.ttl_seconds)
            pipe.expire(self._sessions_index_key(account_id), self.ttl_seconds)
            await pipe.execute()

        items = await self.redis.lrange(hist_key, -count, -1)

        out: list[dict[str, Any]] = []
        for item in items:
            try:
                if isinstance(item, bytes):
                    item = item.decode()
                out.append(json.loads(item))
            except (json.JSONDecodeError, TypeError, ValueError):
                logger.warning(
                    json.dumps(
                        {
                            "event": "history_entry_corrupt",
                            "account_id": account_id,
                            "session_id": session_id,
                        }
                    )
                )
        return out

    async def touch(self, account_id: str, session_id: str) -> None:
        """Refresh the session's TTL."""
        key = self._session_key(account_id, session_id)
        hist_key = self._history_key(account_id, session_id)

        await self.redis.expire(key, self.ttl_seconds)
        await self.redis.expire(hist_key, self.ttl_seconds)
        await self.redis.expire(
            self._sessions_index_key(account_id), self.ttl_seconds
        )

    async def list_sessions(self, account_id: str) -> list[dict[str, Any]]:
        """The owner's live sessions, most recent activity first (metadata only)."""
        index_key = self._sessions_index_key(account_id)
        ids = [
            raw.decode() if isinstance(raw, bytes) else raw
            for raw in await self.redis.smembers(index_key)
        ]
        if not ids:
            return []
        pipe = self.redis.pipeline(transaction=False)
        for sid in ids:
            pipe.hgetall(self._session_key(account_id, sid))
        rows = await pipe.execute()
        sessions = []
        for sid, raw in zip(ids, rows, strict=True):
            record = self._decode_dict(raw)
            if not record:
                continue  # expired; the index sweep removes it
            sessions.append(
                {
                    "id": sid,
                    "created_at": record.get("created_at"),
                    "updated_at": record.get("updated_at"),
                    "turn_count": _int(record.get("turn_count")),
                    "prompt_tokens": _int(record.get("prompt_tokens")),
                    "completion_tokens": _int(record.get("completion_tokens")),
                    "last_model": record.get("last_model"),
                }
            )
        sessions.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
        return sessions

    async def delete_session(self, account_id: str, session_id: str) -> bool:
        """Delete a session and its history; True if it existed.

        A turn still in flight then fails its persistence step instead of
        recreating a partial record.
        """
        async with self.redis.pipeline(transaction=True) as pipe:
            pipe.delete(self._session_key(account_id, session_id))
            pipe.delete(self._history_key(account_id, session_id))
            pipe.srem(self._sessions_index_key(account_id), session_id)
            deleted, _history, _index = await pipe.execute()
        return bool(deleted)

    async def delete_all_sessions(self, account_id: str) -> int:
        """Delete every session the account owns; returns how many were removed."""
        index_key = self._sessions_index_key(account_id)
        ids = [
            raw.decode() if isinstance(raw, bytes) else raw
            for raw in await self.redis.smembers(index_key)
        ]
        removed = 0
        for session_id in ids:
            if await self.delete_session(account_id, session_id):
                removed += 1
        await self.redis.delete(index_key)
        return removed

    async def _cleanup_stale_sessions(self, account_id: str) -> None:
        """Drop expired session IDs from the owner's index.

        Runs on the request path, so it is throttled to once per
        ``CLEANUP_INTERVAL_SECONDS`` per account and checks all members in one
        pipelined round trip.
        """
        swept = await self.redis.set(
            f"pgw:user:{account_id}:sessions:swept", "1", nx=True, ex=CLEANUP_INTERVAL_SECONDS
        )
        if not swept:
            return
        index_key = self._sessions_index_key(account_id)
        member_ids = [
            raw.decode() if isinstance(raw, bytes) else raw
            for raw in await self.redis.smembers(index_key)
        ]
        if not member_ids:
            return
        pipe = self.redis.pipeline(transaction=False)
        for sid in member_ids:
            pipe.exists(self._session_key(account_id, sid))
        alive = await pipe.execute()
        stale = [sid for sid, exists in zip(member_ids, alive, strict=True) if not exists]
        if stale:
            await self.redis.srem(index_key, *stale)

    def _decode_dict(self, data: dict[Any, Any]) -> dict[str, Any]:
        """Decode a Redis hash (bytes or str)."""
        if not data:
            return {}
        return {
            (k.decode() if isinstance(k, bytes) else k): (
                v.decode() if isinstance(v, bytes) else v
            )
            for k, v in data.items()
        }
