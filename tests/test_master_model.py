"""master_model: one model serves every chat request, whatever the client asks for."""
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

import app.router.failover as failover_module
from app.config import ConfigPatch, get_settings
from app.main import app
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.provider.adapter import AttemptResult
from app.store.redis_store import RedisStore
from tests.conftest import _FakeBlockingResponse


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
def served(redis, monkeypatch):
    """Two keys serving different models; records the model each attempt used."""
    models_called: list[str] = []

    async def fake_attempt(reg, key_id, model, messages, **params):
        models_called.append(model)
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)
    return models_called


async def _seed(redis):
    registry = PoolRegistry(redis)
    await registry.create(KeyCreate(provider=Provider.OPENAI, models=["small"], key="k-small"))
    await registry.create(KeyCreate(provider=Provider.GROQ, models=["big"], key="k-big"))


async def _chat(client, model):
    account = await client.post("/v1/accounts", json={})
    api_key = account.json()["keys"][0]["key"]
    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


@pytest.mark.asyncio
async def test_master_model_overrides_the_requested_model(client, redis, served, monkeypatch):
    await _seed(redis)
    monkeypatch.setattr(get_settings(), "master_model", "big")

    resp = await _chat(client, "small")

    assert resp.status_code == 200
    assert served == ["big"]
    assert resp.json()["model"] == "big"


@pytest.mark.asyncio
async def test_master_model_also_catches_models_nobody_serves(client, redis, served, monkeypatch):
    await _seed(redis)
    monkeypatch.setattr(get_settings(), "master_model", "big")

    resp = await _chat(client, "no-such-model")

    assert resp.status_code == 200 and served == ["big"]


@pytest.mark.asyncio
async def test_without_a_master_model_the_requested_model_is_used(client, redis, served, monkeypatch):
    await _seed(redis)
    monkeypatch.setattr(get_settings(), "master_model", None)

    resp = await _chat(client, "small")

    assert resp.status_code == 200 and served == ["small"]


def test_blank_master_model_clears_it_and_null_is_allowed():
    assert ConfigPatch(master_model="  ").model_dump(exclude_unset=True) == {"master_model": None}
    assert ConfigPatch(master_model=" big ").model_dump(exclude_unset=True) == {"master_model": "big"}
    assert ConfigPatch(master_model=None).model_dump(exclude_unset=True) == {"master_model": None}
