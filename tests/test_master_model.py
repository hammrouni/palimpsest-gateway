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


async def _seed_two_providers(redis):
    registry = PoolRegistry(redis)
    await registry.create(KeyCreate(provider=Provider.OPENAI, models=["a1", "a2"], key="k-a"))
    await registry.create(KeyCreate(provider=Provider.GROQ, models=["g1", "g2"], key="k-g"))


@pytest.mark.asyncio
async def test_provider_master_catches_every_model_of_that_provider_only(
    client, redis, served, monkeypatch
):
    await _seed_two_providers(redis)
    monkeypatch.setattr(get_settings(), "master_model", None)
    monkeypatch.setattr(get_settings(), "provider_master_models", {"groq": "g2"})

    to_groq = await _chat(client, "g1")
    other = await _chat(client, "a1")

    assert to_groq.status_code == 200 and other.status_code == 200
    assert served == ["g2", "a1"]  # groq models rerouted, openai untouched


@pytest.mark.asyncio
async def test_gateway_master_wins_over_provider_masters(client, redis, served, monkeypatch):
    await _seed_two_providers(redis)
    monkeypatch.setattr(get_settings(), "master_model", "a2")
    monkeypatch.setattr(get_settings(), "provider_master_models", {"groq": "g2"})

    await _chat(client, "g1")

    assert served == ["a2"]


@pytest.mark.asyncio
async def test_config_only_accepts_masters_the_pool_serves(client, redis, monkeypatch):
    from app.store.operator import OperatorStore

    await _seed_two_providers(redis)
    key = (await OperatorStore(redis).create_key())["key"]
    headers = {"Authorization": f"Bearer {key}"}
    path = "/operator/v1/config"
    monkeypatch.setattr(get_settings(), "master_model", None)
    monkeypatch.setattr(get_settings(), "provider_master_models", {})

    unserved = await client.patch(path, json={"master_model": "nope"}, headers=headers)
    wrong_provider = await client.patch(
        path, json={"provider_master_models": {"groq": "a1"}}, headers=headers
    )
    unknown = await client.patch(
        path, json={"provider_master_models": {"zzz": "a1"}}, headers=headers
    )
    ok = await client.patch(
        path,
        json={"master_model": "a1", "provider_master_models": {"groq": "g1", "openai": ""}},
        headers=headers,
    )
    cleared = await client.patch(path, json={"master_model": None}, headers=headers)

    assert unserved.status_code == 422 and unserved.json()["error"]["param"] == "master_model"
    assert wrong_provider.status_code == 422
    assert unknown.status_code == 422
    assert ok.status_code == 200
    assert ok.json()["provider_master_models"] == {"groq": "g1"}  # blank dropped
    assert cleared.json()["master_model"] is None
