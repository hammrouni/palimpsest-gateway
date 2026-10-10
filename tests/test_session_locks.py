"""Session lock primitive: acquire, release, token checks."""
import pytest
from fakeredis.aioredis import FakeRedis

from app.session.locks import SessionLock


@pytest.fixture
async def lock():
    """Provide SessionLock with FakeRedis."""
    redis = FakeRedis()
    return SessionLock(redis, lock_ttl_ms=10000)


@pytest.mark.asyncio
async def test_acquire_new_lock(lock):
    """First acquire succeeds."""
    success, token = await lock.acquire("lock1")
    assert success is True
    assert token


@pytest.mark.asyncio
async def test_acquire_held_lock_fails(lock):
    """Second acquire with different token fails."""
    success1, token1 = await lock.acquire("lock1")
    assert success1 is True

    success2, token2 = await lock.acquire("lock1")
    assert success2 is False
    assert token1 != token2


@pytest.mark.asyncio
async def test_reentrant_same_token(lock):
    """Re-acquire with same token succeeds (extends TTL)."""
    success1, token = await lock.acquire("lock1")
    assert success1 is True

    success2, _ = await lock.acquire("lock1", token)
    assert success2 is True


@pytest.mark.asyncio
async def test_release_with_correct_token(lock):
    """Release with correct token succeeds."""
    success, token = await lock.acquire("lock1")
    assert success is True

    released = await lock.release("lock1", token)
    assert released is True

    # Can acquire again
    success2, _ = await lock.acquire("lock1")
    assert success2 is True


@pytest.mark.asyncio
async def test_release_with_wrong_token_fails(lock):
    """Release with wrong token fails, lock still held."""
    success, token1 = await lock.acquire("lock1")
    assert success is True

    # Try to release with different token
    released = await lock.release("lock1", "wrong-token")
    assert released is False

    # Lock still held
    is_locked = await lock.is_locked("lock1")
    assert is_locked is True

    # Correct token can release
    released = await lock.release("lock1", token1)
    assert released is True


@pytest.mark.asyncio
async def test_lock_has_ttl(lock):
    """Lock key has TTL set."""
    await lock.acquire("lock1")

    ttl = await lock.redis.pttl("lock1")
    assert ttl > 0
