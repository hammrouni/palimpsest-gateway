"""Operator key endpoints: issue, list, revoke."""
import os

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient

from app.store.operator import OperatorStore


@pytest.fixture(autouse=True)
def _master_key():
    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        os.environ["PALIMPSEST_GATE_MASTER_KEY"] = Fernet.generate_key().decode()


@pytest.fixture
async def operator_headers(fake_redis: FakeRedis) -> dict[str, str]:
    result = await OperatorStore(fake_redis).create_key()
    return {"Authorization": f"Bearer {result['key']}"}


@pytest.mark.asyncio
async def test_create_operator_key_returns_201_pgw_opk_prefix_no_material(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    resp = await async_client.post("/operator/v1/keys", headers=operator_headers)

    assert resp.status_code == 201
    body = resp.json()
    assert body["key"].startswith("pgw_opk_")
    assert "key_material" not in resp.text
    assert "key_hash" not in resp.text


@pytest.mark.asyncio
async def test_list_operator_keys_excludes_raw_and_material(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    create_resp = await async_client.post("/operator/v1/keys", headers=operator_headers)
    raw_key = create_resp.json()["key"]

    list_resp = await async_client.get("/operator/v1/keys", headers=operator_headers)

    assert list_resp.status_code == 200
    assert raw_key not in list_resp.text
    assert "key_material" not in list_resp.text
    ids = [k["id"] for k in list_resp.json()]
    assert create_resp.json()["id"] in ids


@pytest.mark.asyncio
async def test_revoke_operator_key_returns_204_and_is_idempotent(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    create_resp = await async_client.post("/operator/v1/keys", headers=operator_headers)
    key_id = create_resp.json()["id"]

    first = await async_client.delete(f"/operator/v1/keys/{key_id}", headers=operator_headers)
    assert first.status_code == 204

    # Revoking an already-revoked key is idempotent (still 204, not an error).
    second = await async_client.delete(f"/operator/v1/keys/{key_id}", headers=operator_headers)
    assert second.status_code == 204


@pytest.mark.asyncio
async def test_revoke_unknown_operator_key_returns_404(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    resp = await async_client.delete("/operator/v1/keys/does-not-exist", headers=operator_headers)
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_revoked_operator_key_rejected_by_auth(
    async_client: AsyncClient, fake_redis: FakeRedis
):
    """Revocation is immediate: a revoked
    operator key must fail get_current_operator, not just show status=revoked
    in storage."""
    store = OperatorStore(fake_redis)
    await store.create_key()  # another active key must remain: the last one cannot be revoked
    created = await store.create_key()
    await store.revoke_key(created["id"])

    resp = await async_client.get(
        "/operator/v1/keys",
        headers={"Authorization": f"Bearer {created['key']}"},
    )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_missing_credential_returns_401(async_client: AsyncClient):
    resp = await async_client.get("/operator/v1/keys")
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_valid_user_key_on_operator_route_returns_403(async_client: AsyncClient):
    account_resp = await async_client.post("/v1/accounts", json={})
    raw_user_key = account_resp.json()["keys"][0]["key"]

    resp = await async_client.post(
        "/operator/v1/keys",
        headers={"Authorization": f"Bearer {raw_user_key}"},
    )

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "operator_access_denied"


@pytest.mark.asyncio
async def test_valid_operator_key_on_user_route_returns_401(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    """An operator key is unknown in the user realm."""
    resp = await async_client.get("/v1/accounts/does-not-exist/keys", headers=operator_headers)
    assert resp.status_code == 401


@pytest.mark.asyncio
async def test_revoking_the_last_active_operator_key_is_refused_with_409(
    async_client: AsyncClient, fake_redis: FakeRedis
):
    store = OperatorStore(fake_redis)
    only = await store.create_key()
    headers = {"Authorization": f"Bearer {only['key']}"}

    resp = await async_client.delete(f"/operator/v1/keys/{only['id']}", headers=headers)

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "last_operator_key"
    # Still able to authenticate: nobody was locked out.
    assert (await async_client.get("/operator/v1/keys", headers=headers)).status_code == 200


@pytest.mark.asyncio
async def test_bootstrap_seed_restores_access_when_every_key_is_revoked(fake_redis: FakeRedis):
    store = OperatorStore(fake_redis)
    await store.seed_bootstrap_key("env-bootstrap-key")
    spare = await store.create_key()
    bootstrap = next(k for k in await store.list_keys() if k["id"] != spare["id"])
    await store.revoke_key(bootstrap["id"])  # allowed: the spare is still active

    # Simulate the remaining key being removed out-of-band (e.g. Redis edit).
    await fake_redis.hset(store._key_record_key(spare["id"]), "status", "revoked")
    assert await store.active_key_count() == 0

    await store.seed_bootstrap_key("env-bootstrap-key")

    assert await store.active_key_count() == 1
    assert len(await store.list_keys()) == 2  # reactivated, not duplicated


@pytest.mark.asyncio
async def test_bootstrap_seed_is_a_noop_while_an_active_key_exists(fake_redis: FakeRedis):
    store = OperatorStore(fake_redis)
    await store.create_key()
    await store.seed_bootstrap_key("env-bootstrap-key")
    assert len(await store.list_keys()) == 1
