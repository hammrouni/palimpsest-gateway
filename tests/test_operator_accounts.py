"""Operator account management.

Pins the realm boundary, the list's shape and key counts, enable/disable
changing whether an account's keys authenticate, and that no key material
escapes.
"""
from __future__ import annotations

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient

from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore

ACCOUNTS_PATH = "/operator/v1/accounts"

# The operator account routes.
_ADDED_PATHS = {
    "/operator/v1/accounts",
    "/operator/v1/accounts/{account_id}/keys",
    "/operator/v1/accounts/{account_id}/disable",
    "/operator/v1/accounts/{account_id}/enable",
    "/operator/v1/accounts/{account_id}/daily-cap",
    "/operator/v1/accounts/{account_id}/sessions",
    "/operator/v1/accounts/{account_id}",
    "/operator/v1/accounts/{account_id}/stats",
}

_LIST_FIELDS = ("id", "display_name", "created_at", "active_key_count", "status")


@pytest.fixture
async def operator_headers(fake_redis: FakeRedis) -> dict[str, str]:
    return {"Authorization": f"Bearer {(await OperatorStore(fake_redis).create_key())['key']}"}


async def _create_account(fake_redis: FakeRedis, name: str = "alice") -> dict:
    return await RedisStore(fake_redis).create_account(name)


async def _user_headers(fake_redis: FakeRedis) -> dict[str, str]:
    account = await RedisStore(fake_redis).create_account("a user")
    return {"Authorization": f"Bearer {account['keys'][0]['key']}"}


# -- realm boundary ------------------------------------------------


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", ACCOUNTS_PATH),
        ("GET", f"{ACCOUNTS_PATH}/00000000-0000-7000-8000-000000000000/keys"),
        ("POST", f"{ACCOUNTS_PATH}/00000000-0000-7000-8000-000000000000/disable"),
        ("POST", f"{ACCOUNTS_PATH}/00000000-0000-7000-8000-000000000000/enable"),
    ],
)
@pytest.mark.asyncio
async def test_routes_require_an_operator_key(
    async_client: AsyncClient, fake_redis: FakeRedis, method: str, path: str
):
    anonymous = await async_client.request(method, path)
    assert anonymous.status_code == 401

    user_realm = await async_client.request(
        method, path, headers=await _user_headers(fake_redis)
    )
    assert user_realm.status_code == 403
    assert user_realm.json()["error"]["code"] == "operator_access_denied"


# -- list -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_returns_every_account_with_its_shape(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    await _create_account(fake_redis, "alice")
    await _create_account(fake_redis, "bob")

    response = await async_client.get(ACCOUNTS_PATH, headers=operator_headers)

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    for item in body:
        for field in _LIST_FIELDS:
            assert field in item, field
    assert {item["display_name"] for item in body} == {"alice", "bob"}


@pytest.mark.asyncio
async def test_list_counts_only_active_keys(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    store = RedisStore(fake_redis)
    account = await store.create_account("alice")
    extra = await store._create_key(account["id"])

    assert (await async_client.get(ACCOUNTS_PATH, headers=operator_headers)).json()[0][
        "active_key_count"
    ] == 2

    await store.revoke_key(extra["id"], account["id"])

    after = (await async_client.get(ACCOUNTS_PATH, headers=operator_headers)).json()[0]
    assert after["active_key_count"] == 1


@pytest.mark.asyncio
async def test_list_reports_a_disabled_account(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    account = await _create_account(fake_redis, "alice")

    await async_client.post(f"{ACCOUNTS_PATH}/{account['id']}/disable", headers=operator_headers)

    listed = (await async_client.get(ACCOUNTS_PATH, headers=operator_headers)).json()[0]
    assert listed["status"] == "disabled"


# -- an account's keys, prefix only ---------------------------------------


@pytest.mark.asyncio
async def test_key_list_never_carries_key_material(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    account = await _create_account(fake_redis, "alice")

    response = await async_client.get(f"{ACCOUNTS_PATH}/{account['id']}/keys", headers=operator_headers)

    assert response.status_code == 200
    key = response.json()[0]
    assert key["prefix"].startswith("pgw_live_")
    for forbidden in ("key", "key_hash"):
        assert forbidden not in key


@pytest.mark.asyncio
async def test_key_list_404s_for_an_unknown_account(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    response = await async_client.get(
        f"{ACCOUNTS_PATH}/00000000-0000-7000-8000-000000000000/keys", headers=operator_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "account_not_found"


# -- disable actually stops the keys, enable restores them ----------


@pytest.mark.asyncio
async def test_disabling_an_account_makes_its_keys_stop_authenticating(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    account = await RedisStore(fake_redis).create_account("alice")
    user_headers = {"Authorization": f"Bearer {account['keys'][0]['key']}"}
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}

    before = await async_client.post("/v1/chat/completions", json=body, headers=user_headers)
    disabled = await async_client.post(
        f"{ACCOUNTS_PATH}/{account['id']}/disable", headers=operator_headers
    )
    after = await async_client.post("/v1/chat/completions", json=body, headers=user_headers)

    assert before.status_code != 401, "precondition: the key works while active"
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "disabled"
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_enabling_restores_the_keys(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    account = await RedisStore(fake_redis).create_account("alice")
    user_headers = {"Authorization": f"Bearer {account['keys'][0]['key']}"}
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}

    await async_client.post(f"{ACCOUNTS_PATH}/{account['id']}/disable", headers=operator_headers)
    enabled = await async_client.post(
        f"{ACCOUNTS_PATH}/{account['id']}/enable", headers=operator_headers
    )
    after = await async_client.post("/v1/chat/completions", json=body, headers=user_headers)

    assert enabled.status_code == 200
    assert enabled.json()["status"] == "active"
    assert after.status_code != 401


@pytest.mark.asyncio
async def test_disable_is_idempotent(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    account = await _create_account(fake_redis, "alice")

    first = await async_client.post(f"{ACCOUNTS_PATH}/{account['id']}/disable", headers=operator_headers)
    second = await async_client.post(f"{ACCOUNTS_PATH}/{account['id']}/disable", headers=operator_headers)

    assert first.status_code == second.status_code == 200
    assert second.json()["status"] == "disabled"


@pytest.mark.parametrize("action", ["disable", "enable"])
@pytest.mark.asyncio
async def test_status_change_404s_for_an_unknown_account(
    async_client: AsyncClient, operator_headers: dict[str, str], action: str
):
    response = await async_client.post(
        f"{ACCOUNTS_PATH}/00000000-0000-7000-8000-000000000000/{action}", headers=operator_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "account_not_found"


# -- creation reuses the existing self-service endpoint --------------------


@pytest.mark.asyncio
async def test_create_account_returns_the_first_key_exactly_once(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    response = await async_client.post(
        "/v1/accounts", json={"display_name": "carol"}, headers=operator_headers
    )

    assert response.status_code == 201
    created = response.json()
    assert created["keys"][0]["key"].startswith("pgw_live_")

    listed = (await async_client.get(ACCOUNTS_PATH, headers=operator_headers)).json()
    assert [item["display_name"] for item in listed] == ["carol"]
    listed_keys = await async_client.get(
        f"{ACCOUNTS_PATH}/{created['id']}/keys", headers=operator_headers
    )
    assert "key" not in listed_keys.json()[0]


# -- the store seam the routes are built on -------------------------------------


@pytest.mark.asyncio
async def test_create_account_indexes_it_for_the_operator_list(fake_redis: FakeRedis):
    account = await _create_account(fake_redis, "alice")

    indexed = await RedisStore(fake_redis).redis.smembers("pgw:user:accounts")

    assert account["id"] in {
        member.decode() if isinstance(member, bytes) else member for member in indexed
    }


@pytest.mark.asyncio
async def test_list_all_accounts_skips_a_record_that_no_longer_exists(fake_redis: FakeRedis):
    store = RedisStore(fake_redis)
    alive = await store.create_account("alice")
    gone = await store.create_account("bob")
    await store.redis.delete(f"pgw:user:account:{gone['id']}")

    listed = await store.list_all_accounts()

    assert [item["id"] for item in listed] == [alive["id"]]


@pytest.mark.asyncio
async def test_list_all_accounts_rejects_a_malformed_id(fake_redis: FakeRedis):
    store = RedisStore(fake_redis)
    await store.redis.sadd("pgw:user:accounts", "not-a-uuid")

    assert await store.list_all_accounts() == []
    assert await store.set_account_status("not-a-uuid", "disabled") is None


# -- the rest of the API surface -----------------------------------------------------


def test_only_the_documented_routes_were_added(app):
    paths = set(app.openapi()["paths"])

    assert {path for path in paths if path.startswith("/operator/v1/accounts")} == _ADDED_PATHS


# -- the dashboard section ------------------------------------------------


@pytest.mark.asyncio
async def test_bundle_binds_to_the_account_fields_it_renders(async_client: AsyncClient):
    response = await async_client.get("/dashboard/app.js")
    js = response.text

    for field in ("display_name", "active_key_count", "created_at"):
        assert field in js, field


@pytest.mark.asyncio
async def test_bundle_creates_accounts_through_the_self_service_endpoint(
    async_client: AsyncClient,
):
    """Creation stays on ``POST /v1/accounts``; no operator mint endpoint."""
    response = await async_client.get("/dashboard/app.js")
    js = response.text

    assert "'/v1/accounts'" in js
    assert "POST" in js
