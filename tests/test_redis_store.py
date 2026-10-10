"""Tests for Redis store (user realm account/key operations)."""
import pytest
from fakeredis.aioredis import FakeRedis

from app.core.crypto import hash_key
from app.store.redis_store import RedisStore


@pytest.fixture
async def store():
    """Provide RedisStore with fake Redis."""
    redis = FakeRedis()
    return RedisStore(redis)


@pytest.mark.asyncio
async def test_create_account(store):
    """Create account returns account + first key."""
    result = await store.create_account("Test User")
    assert result["id"]
    assert result["display_name"] == "Test User"
    assert result["created_at"]
    assert len(result["keys"]) == 1
    assert result["keys"][0]["key"].startswith("pgw_live_")
    assert result["keys"][0]["account_id"] == result["id"]


@pytest.mark.asyncio
async def test_get_account(store):
    """Get account by ID."""
    created = await store.create_account("Test")
    account_id = created["id"]

    account = await store.get_account(account_id)
    assert account is not None
    assert account["id"] == account_id
    assert account["status"] == "active"


@pytest.mark.asyncio
async def test_account_by_key_hash(store):
    """Resolve account from key hash (auth path)."""
    created = await store.create_account()
    raw_key = created["keys"][0]["key"]
    key_hash = hash_key(raw_key)

    account = await store.account_by_key_hash(key_hash)
    assert account is not None
    assert account["id"] == created["id"]


@pytest.mark.asyncio
async def test_account_by_key_hash_invalid(store):
    """Invalid key hash returns None."""
    result = await store.account_by_key_hash("deadbeef" * 8)
    assert result is None


@pytest.mark.asyncio
async def test_create_key_for_account(store):
    """Create additional key for existing account."""
    created = await store.create_account()
    account_id = created["id"]

    key_record = await store._create_key(account_id)
    assert key_record["id"]
    assert key_record["account_id"] == account_id
    assert key_record["key"].startswith("pgw_live_")


@pytest.mark.asyncio
async def test_list_keys(store):
    """List all keys for account (no raw key or hash)."""
    created = await store.create_account()
    account_id = created["id"]

    await store._create_key(account_id)
    await store._create_key(account_id)

    keys = await store.list_keys(account_id)
    assert len(keys) == 3
    for key in keys:
        assert key["id"]
        assert key["prefix"]
        assert key["created_at"]
        assert key["status"] == "active"
        assert "key" not in key
        assert "key_hash" not in key


@pytest.mark.asyncio
async def test_revoke_key(store):
    """Revoke key immediately."""
    created = await store.create_account()
    account_id = created["id"]
    key_id = created["keys"][0]["id"]
    raw_key = created["keys"][0]["key"]

    revoked = await store.revoke_key(key_id, account_id)
    assert revoked is True

    account = await store.account_by_key_hash(hash_key(raw_key))
    assert account is None


@pytest.mark.asyncio
async def test_revoke_key_idempotent(store):
    """Revoke same key twice returns False on second."""
    created = await store.create_account()
    account_id = created["id"]
    key_id = created["keys"][0]["id"]

    assert await store.revoke_key(key_id, account_id) is True
    assert await store.revoke_key(key_id, account_id) is False


@pytest.mark.asyncio
async def test_revoke_nonexistent_key(store):
    """Revoke nonexistent key returns False."""
    created = await store.create_account()
    result = await store.revoke_key("fake-id", created["id"])
    assert result is False


@pytest.mark.asyncio
async def test_revoke_key_wrong_account_denied(store):
    """Revoking a key via a different account_id is denied (IDOR guard)."""
    created_a = await store.create_account()
    created_b = await store.create_account()
    b_key_id = created_b["keys"][0]["id"]
    b_raw_key = created_b["keys"][0]["key"]

    result = await store.revoke_key(b_key_id, created_a["id"])
    assert result is False

    # B's key remains active
    account = await store.account_by_key_hash(hash_key(b_raw_key))
    assert account is not None


@pytest.mark.asyncio
async def test_update_key_last_used(store):
    """Update last_used_at (best-effort)."""
    created = await store.create_account()
    key_id = created["keys"][0]["id"]

    await store.update_key_last_used(key_id)

    key = await store.get_key(key_id)
    assert key["last_used_at"]


@pytest.mark.asyncio
async def test_update_key_last_used_nonexistent(store):
    """Update last_used_at on nonexistent key doesn't raise."""
    await store.update_key_last_used("fake-id")
