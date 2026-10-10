"""Session lock primitive: owner token, TTL, atomic release."""
import secrets

from redis.asyncio import Redis

from app.session.store import SESSION_LOCK_TTL_SECONDS

# Atomic compare-and-extend: only the current owner may extend its own TTL.
_EXTEND_IF_OWNER_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    redis.call('pexpire', KEYS[1], ARGV[2])
    return 1
else
    return 0
end
"""

# Atomic compare-and-delete: prevents releasing a lock you don't own.
_RELEASE_IF_OWNER_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""


class SessionLock:
    """TTL'd, owner-token'd session lock."""

    def __init__(self, redis: Redis, lock_ttl_ms: int = SESSION_LOCK_TTL_SECONDS * 1000):
        """Initialize.

        Args:
            redis: Async Redis client
            lock_ttl_ms: Lock TTL (default 30s)
        """
        self.redis = redis
        self.lock_ttl_ms = lock_ttl_ms
        self._extend_if_owner = redis.register_script(_EXTEND_IF_OWNER_SCRIPT)
        self._release_if_owner = redis.register_script(_RELEASE_IF_OWNER_SCRIPT)

    async def acquire(self, lock_key: str, token: str | None = None) -> tuple[bool, str]:
        """Acquire a lock with owner token (NX, with TTL).

        Re-acquiring with the same token extends TTL (re-entrant). The
        re-entrant check-and-extend is a single atomic Lua script, so a lock
        that expires and is claimed by a new owner between the failed SET
        and the extend can never have its TTL extended by the old caller.

        Args:
            lock_key: Redis lock key
            token: Owner token (random hex if None)

        Returns:
            (success: bool, token: str)
        """
        if token is None:
            token = secrets.token_hex(16)

        # SET lock_key token NX PX lock_ttl_ms
        result = await self.redis.set(
            lock_key,
            token,
            nx=True,
            px=self.lock_ttl_ms,
        )

        if result:
            return True, token

        # Re-entrant acquire: atomically extend only if we're the current owner.
        extended = await self._extend_if_owner(
            keys=[lock_key], args=[token, self.lock_ttl_ms]
        )
        return bool(extended), token

    async def release(self, lock_key: str, token: str) -> bool:
        """Release a lock (atomic compare-and-delete).

        Only releases if token matches; prevents stale release from handing
        a live session to a waiter.

        Args:
            lock_key: Redis lock key
            token: Owner token

        Returns:
            True if released, False if token mismatch or key not found
        """
        deleted = await self._release_if_owner(keys=[lock_key], args=[token])
        return bool(deleted)

    async def is_locked(self, lock_key: str) -> bool:
        """Check if lock is held.

        Args:
            lock_key: Redis lock key

        Returns:
            True if locked, False otherwise
        """
        return await self.redis.exists(lock_key) > 0
