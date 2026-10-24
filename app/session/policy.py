"""Session turn concurrency: one writer per session, queue briefly then reject."""
import asyncio
import logging
import random
import secrets
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

from redis.asyncio import Redis

from app.config import get_settings
from app.session.locks import SessionLock
from app.session.store import SessionStore

logger = logging.getLogger("palimpsest.session")


class SessionBusy(Exception):
    """Session is locked; brief retry advised.

    ``retry_after_ms`` is the current holder's remaining lock TTL (at least 1).
    """

    def __init__(self, wait_ms: int, retry_after_ms: int | None = None):
        self.wait_ms = wait_ms
        self.retry_after_ms = retry_after_ms if retry_after_ms is not None else wait_ms
        super().__init__(f"Session busy; retry after {self.retry_after_ms}ms")


@dataclass
class TurnLock:
    """A held session lock and its background TTL-renewal task.

    Whoever acquires one via ``acquire_turn_lock`` must call
    ``release_turn_lock`` exactly once, however the turn ends.
    """

    lock: SessionLock
    lock_key: str
    token: str
    renew_task: asyncio.Task | None


async def _renew_loop(lock: SessionLock, lock_key: str, token: str) -> None:
    """Extend the lock every TTL/3 so a long turn never loses it."""
    interval_s = lock.lock_ttl_ms / 3 / 1000.0
    while True:
        await asyncio.sleep(interval_s)
        try:
            renewed, _ = await lock.acquire(lock_key, token)
            if not renewed:
                logger.error(
                    "lock renewal lost ownership for %s — another writer may "
                    "now be active on this session",
                    lock_key,
                )
                return
        except Exception:
            logger.error("lock renewal failed for %s", lock_key, exc_info=True)


def _get_wait_config() -> tuple[int, int]:
    """Get session lock wait/poll config from settings."""
    settings = get_settings()
    return settings.session_lock_wait_ms, settings.session_lock_poll_ms


async def acquire_turn_lock(
    redis: Redis,
    account_id: str,
    session_id: str,
    wait_ms: int | None = None,
    poll_ms: int | None = None,
) -> TurnLock:
    """Acquire the session lock, polling for up to ``wait_ms``.

    Returns:
        A held ``TurnLock``; pass it to ``release_turn_lock`` exactly once.

    Raises:
        SessionBusy: Lock not acquired within wait_ms (caller should return 423)
    """
    if wait_ms is None or poll_ms is None:
        wait_ms, poll_ms = _get_wait_config()

    store = SessionStore(redis)
    lock = SessionLock(redis)
    lock_key = store._lock_key(account_id, session_id)
    token = secrets.token_hex(16)

    acquired, token = await lock.acquire(lock_key, token)
    if not acquired:
        deadline = time.monotonic() + wait_ms / 1000.0
        while not acquired:
            remaining_s = deadline - time.monotonic()
            if remaining_s <= 0:
                # PTTL returns -1/-2 for no expiry / no key: not a useful hint.
                ttl_ms = max(1, int(await redis.pttl(lock_key)))
                raise SessionBusy(wait_ms=wait_ms, retry_after_ms=ttl_ms)
            # Jitter so waiters do not retry in lockstep after a release.
            sleep_s = min(poll_ms / 1000.0, remaining_s) * random.uniform(0.5, 1.0)
            await asyncio.sleep(sleep_s)
            acquired, token = await lock.acquire(lock_key, token)

    renew_task = asyncio.create_task(_renew_loop(lock, lock_key, token))
    return TurnLock(lock=lock, lock_key=lock_key, token=token, renew_task=renew_task)


async def release_turn_lock(turn_lock: TurnLock) -> None:
    """Stop TTL renewal and release the lock. Safe to call more than once."""
    if turn_lock.renew_task is not None:
        turn_lock.renew_task.cancel()
        try:
            await turn_lock.renew_task
        except asyncio.CancelledError:
            pass
        except Exception:
            # A failed renew task must not stop the release below.
            logger.error("lock renew task failed for %s", turn_lock.lock_key, exc_info=True)

    try:
        await turn_lock.lock.release(turn_lock.lock_key, turn_lock.token)
    except Exception:
        logger.error(
            "lock release failed for %s — will expire via TTL",
            turn_lock.lock_key,
            exc_info=True,
        )


@asynccontextmanager
async def session_turn_guard(
    redis: Redis,
    account_id: str,
    session_id: str,
    wait_ms: int | None = None,
    poll_ms: int | None = None,
):
    """Hold the session lock for the block, always releasing it.

    Yields:
        The lock owner token

    Raises:
        SessionBusy: Lock not acquired within wait_ms (caller should return 423)
    """
    turn_lock = await acquire_turn_lock(redis, account_id, session_id, wait_ms, poll_ms)
    try:
        yield turn_lock.token
    finally:
        await release_turn_lock(turn_lock)
