"""Operator realm tests (encryption, realm separation, secret hygiene)."""
import hashlib
import hmac
import os

import pytest
from fakeredis.aioredis import FakeRedis

from app.security.secrets import SecretsManager
from app.store.operator import OperatorStore


@pytest.fixture
def master_key():
    """Generate a test Fernet key."""
    from cryptography.fernet import Fernet
    return Fernet.generate_key().decode()


@pytest.fixture
def secrets_manager(master_key):
    """Provide SecretsManager."""
    return SecretsManager(master_key)


@pytest.fixture
async def operator_store(master_key):
    """Provide OperatorStore with FakeRedis."""
    os.environ["PALIMPSEST_GATE_MASTER_KEY"] = master_key
    redis = FakeRedis()
    return OperatorStore(redis)


@pytest.mark.asyncio
async def test_encrypt_at_rest(secrets_manager):
    """Raw key encrypted, never stored plaintext."""
    raw = "test-secret-key-12345"
    ciphertext = secrets_manager.encrypt(raw)

    assert ciphertext != raw
    assert "test-secret" not in ciphertext


@pytest.mark.asyncio
async def test_decrypt_ttl_none(secrets_manager):
    """Decrypt with ttl=None for persisted credentials."""
    raw = "test-secret"
    ciphertext = secrets_manager.encrypt(raw)

    # Decrypt must use ttl=None to work with old tokens
    decrypted = secrets_manager.decrypt(ciphertext)
    assert decrypted == raw


@pytest.mark.asyncio
async def test_create_operator_key_returns_raw_once(operator_store):
    """Raw key returned exactly once. The operator prefix is pgw_opk_, distinct
    from user-realm pgw_live_ keys."""
    result = await operator_store.create_key()

    assert result["key"].startswith("pgw_opk_")
    assert result["id"]
    assert result["prefix"]
    assert result["created_at"]


@pytest.mark.asyncio
async def test_stored_key_material_is_encrypted(operator_store):
    """Stored key_material is Fernet ciphertext, not raw."""
    result = await operator_store.create_key()
    raw_key = result["key"]
    key_id = result["id"]

    # Read directly from Redis
    record = await operator_store.redis.hgetall(
        f"pgw:operator:api_key:{key_id}"
    )
    record_decoded = {
        (k.decode() if isinstance(k, bytes) else k): (
            v.decode() if isinstance(v, bytes) else v
        )
        for k, v in record.items()
    }

    stored_material = record_decoded["key_material"]
    assert stored_material != raw_key
    assert "pgw_opk" not in stored_material


@pytest.mark.asyncio
async def test_list_never_returns_key_material(operator_store):
    """List response has no key_material or key_hash."""
    await operator_store.create_key()
    await operator_store.create_key()

    keys = await operator_store.list_keys()
    assert len(keys) == 2

    for key in keys:
        assert "id" in key
        assert "prefix" in key
        assert "created_at" in key
        assert "key_material" not in key
        assert "key_hash" not in key
        assert "key" not in key


@pytest.mark.asyncio
async def test_get_by_hash_lookup(operator_store):
    """Edge auth: hash lookup finds key."""
    result = await operator_store.create_key()
    raw_key = result["key"]

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    record = await operator_store.get_by_hash(key_hash)

    assert record is not None
    assert record["status"] == "active"


@pytest.mark.asyncio
async def test_revoke_immediate_effect(operator_store):
    """Revocation immediate."""
    await operator_store.create_key()  # spare: the last active key cannot be revoked
    result = await operator_store.create_key()
    key_id = result["id"]
    raw_key = result["key"]

    # Revoke
    await operator_store.revoke_key(key_id)

    # Verify revoked
    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()
    record = await operator_store.get_by_hash(key_hash)
    assert record["status"] == "revoked"


@pytest.mark.asyncio
async def test_constant_time_compare(operator_store):
    """Edge auth: use constant-time HMAC compare."""
    result = await operator_store.create_key()
    raw_key = result["key"]
    key_id = result["id"]

    # Decrypt stored ciphertext
    decrypted = await operator_store.get_decrypted_key(key_id)

    # Constant-time compare
    assert hmac.compare_digest(raw_key, decrypted)


@pytest.mark.asyncio
async def test_realm_namespace_isolation(operator_store):
    """Operator keys use pgw:operator namespace (separate from user realm)."""
    await operator_store.create_key()

    # Verify key lives in operator namespace
    keys = await operator_store.redis.keys("pgw:operator:*")
    assert len(keys) > 0

    # No user-realm keys should be created by operator seeding
    _ = await operator_store.redis.keys("pgw:user:*")


@pytest.mark.asyncio
async def test_operator_key_not_in_user_realm(operator_store):
    """Operator key lookup in user realm returns nothing."""
    op_result = await operator_store.create_key()
    raw_key = op_result["key"]

    key_hash = hashlib.sha256(raw_key.encode()).hexdigest()

    # Try to find in user realm index (should not exist)
    user_index_key = f"pgw:user:api_key:by_hash:{key_hash}"
    user_lookup = await operator_store.redis.get(user_index_key)

    assert user_lookup is None


@pytest.mark.asyncio
async def test_secret_never_in_list_response(operator_store, caplog):
    """No raw key/hash in list response or logs."""
    result = await operator_store.create_key()
    raw_key = result["key"]

    # List
    keys = await operator_store.list_keys()
    response_str = str(keys)

    assert raw_key not in response_str
    # Check logs too (canary)
    assert raw_key not in caplog.text


@pytest.mark.asyncio
async def test_seed_bootstrap_key_creates_when_none_exist(operator_store):
    """Bootstrap seeds a key from a raw env value when none exists yet."""
    raw = "env-provided-bootstrap-key"

    await operator_store.seed_bootstrap_key(raw)

    keys = await operator_store.list_keys()
    assert len(keys) == 1

    key_hash = hashlib.sha256(raw.encode()).hexdigest()
    record = await operator_store.get_by_hash(key_hash)
    assert record is not None
    assert record["status"] == "active"


@pytest.mark.asyncio
async def test_seed_bootstrap_key_is_idempotent(operator_store):
    """Bootstrap is a no-op once any operator key already exists."""
    await operator_store.create_key()

    await operator_store.seed_bootstrap_key("env-provided-bootstrap-key")

    keys = await operator_store.list_keys()
    assert len(keys) == 1


@pytest.mark.asyncio
async def test_get_decrypted_key_returns_none_for_revoked_key(operator_store):
    """Defensive gate: get_decrypted_key refuses to decrypt a revoked record,
    independent of any caller-side status check."""
    await operator_store.create_key()  # spare: the last active key cannot be revoked
    result = await operator_store.create_key()
    key_id = result["id"]

    await operator_store.revoke_key(key_id)

    assert await operator_store.get_decrypted_key(key_id) is None


# -- list_keys skips malformed records ---------------------------


@pytest.mark.asyncio
async def test_list_keys_skips_malformed_record_missing_required_field(operator_store, caplog):
    """A malformed key record (missing 'id') is skipped, not crashed."""
    good = await operator_store.create_key()
    good_id = good["id"]

    bad_id = "pgw_bad_key_id_1"
    await operator_store.redis.sadd("pgw:operator:api_keys", bad_id)
    await operator_store.redis.hset(
        f"pgw:operator:api_key:{bad_id}",
        mapping={"prefix": "pgw_opk", "status": "active"},
    )

    with caplog.at_level("WARNING", logger="palimpsest.operator"):
        keys = await operator_store.list_keys()

    ids = [k["id"] for k in keys]
    assert good_id in ids
    assert bad_id not in ids
    assert any("operator_key_corrupt" in record.message for record in caplog.records)


@pytest.mark.asyncio
async def test_list_keys_skips_empty_hgetall_result(operator_store, caplog):
    """A key_id in the set whose hash record is empty is skipped."""
    good = await operator_store.create_key()
    good_id = good["id"]

    empty_id = "pgw_empty_key_id"
    await operator_store.redis.sadd("pgw:operator:api_keys", empty_id)
    # No hset — hgetall returns empty dict, _decode_dict returns {} -> `if decoded` is False.

    keys = await operator_store.list_keys()
    ids = [k["id"] for k in keys]
    assert good_id in ids
    assert empty_id not in ids


@pytest.mark.asyncio
async def test_list_keys_skips_partial_record_missing_status(operator_store, caplog):
    """A record missing 'status' (a required field) is skipped, not crashed."""
    good = await operator_store.create_key()
    good_id = good["id"]

    partial_id = "pgw_partial_key_id"
    await operator_store.redis.sadd("pgw:operator:api_keys", partial_id)
    await operator_store.redis.hset(
        f"pgw:operator:api_key:{partial_id}",
        mapping={"id": partial_id, "prefix": "pgw_opk"},
    )

    with caplog.at_level("WARNING", logger="palimpsest.operator"):
        keys = await operator_store.list_keys()

    ids = [k["id"] for k in keys]
    assert good_id in ids
    assert partial_id not in ids
    assert any("operator_key_corrupt" in record.message for record in caplog.records)
