"""Session concurrency: lock acquire/release and queue-then-reject."""
import pytest
from fakeredis.aioredis import FakeRedis

from app.session.locks import SessionLock
from app.session.policy import SessionBusy, session_turn_guard
from app.session.store import SessionStore


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.mark.asyncio
async def test_lock_acquire_release(redis):
    """Acquire and release session lock."""
    store = SessionStore(redis)
    lock = SessionLock(redis)
    lock_key = store._lock_key("account1", "session1")

    # Acquire
    acquired, token = await lock.acquire(lock_key)
    assert acquired
    assert token

    # Lock is held
    assert await lock.is_locked(lock_key)

    # Release
    released = await lock.release(lock_key, token)
    assert released

    # Lock is free
    assert not await lock.is_locked(lock_key)


@pytest.mark.asyncio
async def test_guard_immediate_acquire(redis):
    """Guard acquires lock immediately when free."""
    async with session_turn_guard(redis, "account1", "session1") as token:
        assert token
        # Lock should be held during context
        store = SessionStore(redis)
        lock_key = store._lock_key("account1", "session1")
        assert await SessionLock(redis).is_locked(lock_key)

    # Lock released after context
    store = SessionStore(redis)
    lock_key = store._lock_key("account1", "session1")
    assert not await SessionLock(redis).is_locked(lock_key)


@pytest.mark.asyncio
async def test_guard_queue_then_reject(redis):
    """Contention → brief queue, then 423 rejection."""
    import time

    store = SessionStore(redis)
    lock = SessionLock(redis)
    lock_key = store._lock_key("account1", "session1")

    # Holder: acquire and hold
    holder_token = (await lock.acquire(lock_key))[1]
    start = time.time()

    # Waiter: try to acquire within short budget
    try:
        async with session_turn_guard(redis, "account1", "session1", wait_ms=300, poll_ms=50):
            pass
        raise AssertionError("Should have raised SessionBusy")
    except SessionBusy as e:
        elapsed = (time.time() - start) * 1000
        assert elapsed >= 300  # Waited at least the budget
        assert e.wait_ms == 300

    # Holder still holds the lock
    assert await lock.is_locked(lock_key)

    # Release holder
    await lock.release(lock_key, holder_token)
    assert not await lock.is_locked(lock_key)


@pytest.mark.asyncio
async def test_stale_release_rejected(redis):
    """Stale release (wrong token) is rejected."""
    store = SessionStore(redis)
    lock = SessionLock(redis)
    lock_key = store._lock_key("account1", "session1")

    # Acquire with token A
    acquired_a, token_a = await lock.acquire(lock_key)
    assert acquired_a

    # Try to release with wrong token B
    released = await lock.release(lock_key, "wrong_token")
    assert not released  # Stale release rejected

    # Lock still held by token A
    assert await lock.is_locked(lock_key)

    # Release with correct token
    released = await lock.release(lock_key, token_a)
    assert released
    assert not await lock.is_locked(lock_key)


@pytest.mark.asyncio
async def test_k_parallel_turns_no_corruption(redis):
    """k=5 parallel turns on one session → 5 exchanges exactly once each.

    All turns should complete successfully with the lock serializing them.
    Each turn's canary appears exactly once in the history.
    turn_count advances by exactly 5.
    No exchanges are duplicated or corrupted.
    """
    import asyncio

    store = SessionStore(redis)
    account_id = "account1"
    session_id = "session1"

    # Setup session
    await store.create_or_claim(account_id, session_id)

    async def turn(turn_num: int):
        """Simulate a turn: acquire lock (retrying on contention, like the
        real chat path) → append exchange."""
        # wait_ms comfortably above the worst-case serialized total for 5
        # turns at ~10ms of simulated work each.
        async with session_turn_guard(redis, account_id, session_id, wait_ms=2000, poll_ms=10):
            # Simulate some work
            await asyncio.sleep(0.01)

            # Append exchange with canary
            message = {"role": "user", "content": f"CANARY-turn-{turn_num}"}
            await store.append_exchange(account_id, session_id, [message])
            await store.touch(account_id, session_id)

            return turn_num

    # Run 5 parallel turns (lock will serialize them)
    results = await asyncio.gather(*[turn(i) for i in range(5)])

    # All should complete (no None values)
    assert all(r is not None for r in results)

    # Verify 5 exchanges appended
    exchanges = await store.read_recent(account_id, session_id, count=10)
    assert len(exchanges) == 5

    # Verify each canary appears exactly once
    canaries = {}
    for exchange in exchanges:
        for msg in exchange.get("messages", []):
            content = msg.get("content", "")
            if "CANARY-turn-" in content:
                canaries[content] = canaries.get(content, 0) + 1

    # Each turn's canary appears exactly once
    for i in range(5):
        canary = f"CANARY-turn-{i}"
        assert canaries.get(canary) == 1, f"{canary} count: {canaries.get(canary)}"

    # Turn count advanced by 5
    metadata = await store.get_metadata(account_id, session_id)
    turn_count = int(metadata.get("turn_count", 0))
    assert turn_count == 5


# -- SessionBusy retry_after_ms reflects the real remaining lock TTL ------


@pytest.mark.asyncio
async def test_session_busy_retry_after_reflects_real_lock_ttl(redis):
    """When the wait budget is exhausted, SessionBusy.retry_after_ms
    is the current holder's actual remaining lock TTL (in ms), not the
    static wait_ms budget."""
    store = SessionStore(redis)
    lock = SessionLock(redis)
    lock_key = store._lock_key("account1", "session1")

    # Holder acquires the lock — full TTL (~30s by default).
    await lock.acquire(lock_key)
    real_remaining_s = await redis.ttl(lock_key)
    assert real_remaining_s > 0

    # Waiter exhausts its short wait budget quickly.
    try:
        async with session_turn_guard(
            redis, "account1", "session1", wait_ms=200, poll_ms=50
        ):
            pass
        raise AssertionError("should have raised SessionBusy")
    except SessionBusy as busy:
        # The retry hint is the real lock TTL (clamped to >= 1ms), not
        # the wait budget (200ms).
        assert busy.retry_after_ms >= 1
        assert busy.retry_after_ms > busy.wait_ms, (
            f"retry_after_ms {busy.retry_after_ms} should exceed wait_ms {busy.wait_ms}"
        )
        # And it should be close to the real remaining TTL (within 1s of slop).
        assert abs(busy.retry_after_ms - real_remaining_s * 1000) < 1000


@pytest.mark.asyncio
async def test_session_busy_default_retry_after_is_wait_ms(redis):
    """Without retry_after_ms, it defaults to wait_ms."""
    busy = SessionBusy(wait_ms=500)
    assert busy.retry_after_ms == 500
    assert busy.wait_ms == 500
