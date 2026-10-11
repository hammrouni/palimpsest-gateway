"""GET /v1/models: OpenAI-shaped, authenticated, and non-disclosing."""
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.store.redis_store import RedisStore


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _headers(client) -> dict:
    body = (await client.post("/v1/accounts", json={})).json()
    return {"Authorization": f"Bearer {body['keys'][0]['key']}"}


@pytest.mark.asyncio
async def test_lists_distinct_models_without_provider_or_key_details(client, redis):
    registry = PoolRegistry(redis)
    await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini", "gpt-4o"], key="sk-secret-aaa")
    )
    await registry.create(
        KeyCreate(provider=Provider.OPENROUTER, models=["gpt-4o-mini", "meta/llama-3"], key="sk-secret-bbb")
    )

    resp = await client.get("/v1/models", headers=await _headers(client))

    assert resp.status_code == 200
    body = resp.json()
    assert body["object"] == "list"
    assert [m["id"] for m in body["data"]] == ["gpt-4o", "gpt-4o-mini", "meta/llama-3"]
    assert all(m["object"] == "model" and m["owned_by"] == "palimpsest" for m in body["data"])
    assert all(isinstance(m["created"], int) for m in body["data"])
    for secret in ("openai", "openrouter", "sk-secret", "active", "key_id"):
        assert secret not in resp.text.lower()


@pytest.mark.asyncio
async def test_suspended_keys_models_are_not_listed(client, redis):
    registry = PoolRegistry(redis)
    live = await registry.create(KeyCreate(provider=Provider.OPENAI, models=["live-model"], key="sk-a"))
    gone = await registry.create(KeyCreate(provider=Provider.OPENAI, models=["paused-model"], key="sk-b"))
    await registry.suspend(gone.id)

    data = (await client.get("/v1/models", headers=await _headers(client))).json()["data"]

    assert [m["id"] for m in data] == ["live-model"]
    assert live.id  # silence unused


@pytest.mark.asyncio
async def test_empty_pool_is_an_empty_list(client):
    resp = await client.get("/v1/models", headers=await _headers(client))
    assert resp.json() == {"object": "list", "data": []}


@pytest.mark.asyncio
async def test_retrieve_one_model_including_slashed_ids(client, redis):
    await PoolRegistry(redis).create(
        KeyCreate(provider=Provider.OPENROUTER, models=["meta/llama-3"], key="sk-a")
    )
    headers = await _headers(client)

    found = await client.get("/v1/models/meta/llama-3", headers=headers)
    assert found.status_code == 200 and found.json()["id"] == "meta/llama-3"

    missing = await client.get("/v1/models/nope", headers=headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "model_not_found"


@pytest.mark.asyncio
async def test_requires_a_user_key(client):
    assert (await client.get("/v1/models")).status_code == 401
    assert (await client.get("/v1/models/x")).status_code == 401
