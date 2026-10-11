"""PATCH /v1/operator/pool/keys/{id}: edit models and daily cap in place."""
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore

BASE = "/v1/operator/pool/keys"


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def headers(redis):
    return {"Authorization": f"Bearer {(await OperatorStore(redis).create_key())['key']}"}


async def _key(redis, **kwargs) -> str:
    data = {"provider": Provider.OPENROUTER, "models": ["a"], "key": "sk-or-material", **kwargs}
    return (await PoolRegistry(redis).create(KeyCreate(**data))).id


@pytest.mark.asyncio
async def test_models_can_be_replaced_and_become_routable(client, redis, headers):
    key_id = await _key(redis)

    resp = await client.patch(f"{BASE}/{key_id}", headers=headers, json={"models": ["a", " b ", "b", "c/d:free"]})

    assert resp.status_code == 200
    assert resp.json()["models"] == ["a", "b", "c/d:free"]  # trimmed, de-duplicated, ordered
    registry = PoolRegistry(redis)
    assert [k.id for k in await registry.routable_keys(model="c/d:free")] == [key_id]
    assert (await client.get(f"{BASE}/{key_id}", headers=headers)).json()["models"] == ["a", "b", "c/d:free"]


@pytest.mark.asyncio
async def test_removed_models_stop_routing(client, redis, headers):
    key_id = await _key(redis, models=["a", "b"])

    await client.patch(f"{BASE}/{key_id}", headers=headers, json={"models": ["b"]})

    registry = PoolRegistry(redis)
    assert await registry.routable_keys(model="a") == []
    assert [k.id for k in await registry.routable_keys(model="b")] == [key_id]


@pytest.mark.asyncio
async def test_daily_cap_set_changed_and_cleared_with_null(client, redis, headers):
    key_id = await _key(redis)

    assert (await client.patch(f"{BASE}/{key_id}", headers=headers, json={"daily_cap": 50})).json()["daily_cap"] == 50
    # Editing models alone leaves the cap alone.
    kept = (await client.patch(f"{BASE}/{key_id}", headers=headers, json={"models": ["z"]})).json()
    assert kept["daily_cap"] == 50 and kept["models"] == ["z"]
    cleared = (await client.patch(f"{BASE}/{key_id}", headers=headers, json={"daily_cap": None})).json()
    assert cleared["daily_cap"] is None


@pytest.mark.asyncio
async def test_edit_keeps_state_and_never_exposes_material(client, redis, headers):
    key_id = await _key(redis)
    await PoolRegistry(redis).suspend(key_id)

    resp = await client.patch(f"{BASE}/{key_id}", headers=headers, json={"models": ["b"]})

    assert resp.json()["state"] == "suspended"  # an edit is not a resume
    assert "sk-or-material" not in resp.text
    assert resp.json()["last_modified_by"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "body",
    [{}, {"models": []}, {"models": ["ok", "  "]}, {"models": None}, {"daily_cap": -1}, {"provider": "openai"}],
)
async def test_invalid_edits_are_rejected_without_changing_anything(client, redis, headers, body):
    key_id = await _key(redis, models=["keep"])

    resp = await client.patch(f"{BASE}/{key_id}", headers=headers, json=body)

    assert resp.status_code == 422
    assert (await client.get(f"{BASE}/{key_id}", headers=headers)).json()["models"] == ["keep"]


@pytest.mark.asyncio
async def test_unknown_key_404_and_auth_required(client, redis, headers):
    key_id = await _key(redis)

    assert (await client.patch(f"{BASE}/nope", headers=headers, json={"models": ["a"]})).status_code == 404
    assert (await client.patch(f"{BASE}/{key_id}", json={"models": ["a"]})).status_code == 401
