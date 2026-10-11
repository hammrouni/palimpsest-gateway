"""Config validation and audit; account and operator store edge cases."""
from __future__ import annotations

import json
import logging

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient
from pydantic import ValidationError

from app.config import ConfigPatch, apply_persisted_overrides, get_settings
from app.store.config import ConfigStore
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore

CONFIG_PATH = "/operator/v1/config"


@pytest.fixture(autouse=True)
def restore_settings():
    settings = get_settings()
    before = settings.model_dump()
    yield
    for field, value in before.items():
        setattr(settings, field, value)


@pytest.fixture
async def operator_headers(fake_redis: FakeRedis) -> dict[str, str]:
    created = await OperatorStore(fake_redis).create_key()
    return {"Authorization": f"Bearer {created['key']}"}


# -- config: null / coercion / audit / persist-first ------------------------------


@pytest.mark.parametrize(
    "field",
    [
        "verbatim_turns_n",
        "retry_budget",
        "request_log_prompts",
        "cooldown_seconds",
        "failover_event_log_size",
    ],
)
def test_null_is_rejected_for_every_required_tunable(field):
    with pytest.raises(ValidationError):
        ConfigPatch(**{field: None})


def test_null_is_accepted_only_for_user_daily_cap():
    assert ConfigPatch(user_daily_cap=None).model_dump(exclude_unset=True) == {"user_daily_cap": None}


@pytest.mark.asyncio
async def test_patch_with_null_required_setting_is_422_and_changes_nothing(
    async_client: AsyncClient, operator_headers
):
    before = get_settings().retry_budget
    resp = await async_client.patch(CONFIG_PATH, json={"retry_budget": None}, headers=operator_headers)
    assert resp.status_code == 422
    assert get_settings().retry_budget == before


def test_persisted_override_is_applied_in_its_coerced_type_and_null_is_skipped():
    applied = apply_persisted_overrides({"verbatim_turns_n": "6", "retry_budget": None})
    assert applied == {"verbatim_turns_n": 6}
    assert isinstance(get_settings().verbatim_turns_n, int)


@pytest.mark.asyncio
async def test_route_audit_records_the_operator_and_the_value_actually_in_effect(
    async_client: AsyncClient, operator_headers, fake_redis, caplog
):
    operator_id = (await OperatorStore(fake_redis).list_keys())[0]["id"]
    original = get_settings().verbatim_turns_n  # an env default: no override stored yet

    with caplog.at_level(logging.INFO, logger="palimpsest.config"):
        first = await async_client.patch(CONFIG_PATH, json={"verbatim_turns_n": original + 2}, headers=operator_headers)
        second = await async_client.patch(CONFIG_PATH, json={"verbatim_turns_n": original + 5}, headers=operator_headers)
    assert first.status_code == second.status_code == 200

    records = [json.loads(r.message) for r in caplog.records if r.name == "palimpsest.config"]
    assert [r["operator_id"] for r in records] == [operator_id, operator_id]
    assert records[0]["changes"] == [{"key": "verbatim_turns_n", "old": original, "new": original + 2}]
    assert records[1]["changes"] == [{"key": "verbatim_turns_n", "old": original + 2, "new": original + 5}]


@pytest.mark.asyncio
async def test_a_failed_persist_leaves_the_running_settings_untouched(
    async_client: AsyncClient, operator_headers, monkeypatch
):
    async def broken(self, *a, **kw):
        raise RuntimeError("redis write failed")

    monkeypatch.setattr(ConfigStore, "set_overrides", broken)
    before = get_settings().cooldown_seconds
    # The test transport re-raises the server-side error (production answers 500).
    with pytest.raises(RuntimeError):
        await async_client.patch(
            CONFIG_PATH, json={"cooldown_seconds": before + 30}, headers=operator_headers
        )
    assert get_settings().cooldown_seconds == before  # not live-but-unpersisted


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"retry_budget": 21},
        {"failover_event_log_size": 1_000_001},
        {"failover_event_log_size": 0},
        {"request_log_ttl_hours": 8761},
        {"request_log_ttl_hours": 0},
        {"verbatim_turns_n": "many"},
        {"retry_budget": "many"},
    ],
)
async def test_config_bounds_are_enforced_and_nothing_is_persisted(
    async_client: AsyncClient, operator_headers, fake_redis, payload
):
    resp = await async_client.patch(CONFIG_PATH, json=payload, headers=operator_headers)
    assert resp.status_code == 422
    assert await ConfigStore(fake_redis).get_overrides() == {}


# -- account store ----------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_all_accounts_is_oldest_first(fake_redis: FakeRedis):
    store = RedisStore(fake_redis)
    ids = {}
    for name, created in (("late", "2026-03-01T00:00:00+00:00"), ("early", "2026-01-01T00:00:00+00:00"),
                          ("mid", "2026-02-01T00:00:00+00:00")):
        account = await store.create_account(name)
        ids[name] = account["id"]
        await fake_redis.hset(f"pgw:user:account:{account['id']}", "created_at", created)

    listed = await store.list_all_accounts()
    assert [a["display_name"] for a in listed] == ["early", "mid", "late"]


@pytest.mark.asyncio
async def test_set_account_status_rejects_unknown_status(fake_redis: FakeRedis):
    store = RedisStore(fake_redis)
    account = await store.create_account("x")
    with pytest.raises(ValueError):
        await store.set_account_status(account["id"], "banned")
    assert (await store.get_account(account["id"]))["status"] == "active"


@pytest.mark.asyncio
async def test_a_malformed_key_record_does_not_break_the_account_list(fake_redis: FakeRedis):
    store = RedisStore(fake_redis)
    account = await store.create_account("x")
    await fake_redis.sadd(f"pgw:user:account:{account['id']}:keys", "broken-key")
    await fake_redis.hset("pgw:user:api_key:broken-key", "id", "broken-key")  # no prefix/status

    listed = await store.list_all_accounts()
    assert len(listed) == 1 and listed[0]["active_key_count"] == 1


@pytest.mark.asyncio
async def test_last_used_update_never_recreates_a_vanished_key_record(fake_redis: FakeRedis):
    store = RedisStore(fake_redis)
    await store.update_key_last_used("ghost")
    assert not await fake_redis.exists("pgw:user:api_key:ghost")


@pytest.mark.asyncio
async def test_key_record_without_account_id_is_a_401_not_a_500(fake_redis: FakeRedis):
    store = RedisStore(fake_redis)
    await fake_redis.set("pgw:user:api_key:by_hash:h1", "k1")
    await fake_redis.hset("pgw:user:api_key:k1", mapping={"id": "k1", "status": "active"})
    assert await store.account_by_key_hash("h1") is None


@pytest.mark.asyncio
async def test_account_without_status_is_listed_as_unknown_not_active(
    async_client: AsyncClient, operator_headers, fake_redis: FakeRedis
):
    store = RedisStore(fake_redis)
    account = await store.create_account("no-status")
    await fake_redis.hdel(f"pgw:user:account:{account['id']}", "status")

    resp = await async_client.get("/operator/v1/accounts", headers=operator_headers)
    assert resp.status_code == 200
    assert resp.json()[0]["status"] == "unknown"  # auth rejects it, so it must not read "active"


# -- operator keys ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_revoking_an_id_that_addresses_another_key_type_is_404_not_500(
    async_client: AsyncClient, operator_headers
):
    resp = await async_client.delete("/operator/v1/keys/by_hash:abc", headers=operator_headers)
    assert resp.status_code == 404
    resp = await async_client.delete("/operator/v1/keys/" + "x" * 80, headers=operator_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_reactivating_the_bootstrap_key_is_logged(fake_redis: FakeRedis, caplog):
    store = OperatorStore(fake_redis)
    await store.seed_bootstrap_key("env-bootstrap-key")
    spare = await store.create_key()
    boot = next(k for k in await store.list_keys() if k["id"] != spare["id"])
    await store.revoke_key(boot["id"])
    await fake_redis.hset(store._key_record_key(spare["id"]), "status", "revoked")

    with caplog.at_level(logging.WARNING, logger="palimpsest.operator"):
        await store.seed_bootstrap_key("env-bootstrap-key")
    assert "operator_bootstrap_key_reactivated" in caplog.text
