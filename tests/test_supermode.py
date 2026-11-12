"""Supermode: ``palimpsest/supermode`` lets the gateway pick the best model."""
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

import app.router.failover as failover_module
from app.config import get_settings
from app.main import app
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.provider.adapter import AttemptResult, ProviderAttemptError
from app.router.supermode import Needs, is_supermode, needs_of, rank_models
from app.router.supermode_profiles import UNKNOWN, profile_for
from app.schemas.chat import ChatCompletionRequest
from app.store.redis_store import RedisStore
from tests.conftest import _FakeBlockingResponse

_SMALL = Needs(tokens=100, vision=False, tools=False)


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _no_masters(monkeypatch):
    monkeypatch.setattr(get_settings(), "master_model", None)
    monkeypatch.setattr(get_settings(), "provider_master_models", {})
    monkeypatch.setattr(get_settings(), "supermode_quality", {})


async def _key(redis, provider, models, key):
    return await PoolRegistry(redis).create(KeyCreate(provider=provider, models=models, key=key))


async def _chat(client, body):
    account = await client.post("/v1/accounts", json={})
    api_key = account.json()["keys"][0]["key"]
    return await client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=body
    )


def _hi(**extra):
    return {"model": "palimpsest/supermode", "messages": [{"role": "user", "content": "hi"}], **extra}


def test_profiles_match_ids_with_routing_prefixes_and_variants():
    assert profile_for("anthropic/claude-opus-4.1").quality > profile_for("gpt-4o").quality
    assert profile_for("openai/gpt-4o-mini:free") == profile_for("gpt-4o-mini")
    assert profile_for("gpt-4o-mini").quality < profile_for("gpt-4o").quality
    assert profile_for("totally-new-model") == UNKNOWN


def test_is_supermode_ignores_case_and_spaces():
    assert is_supermode(" Palimpsest/SuperMode ")
    assert not is_supermode("palimpsest/other")


def test_needs_detect_images_tools_and_reply_room():
    request = ChatCompletionRequest(
        model="palimpsest/supermode",
        messages=[
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
                ],
            }
        ],
        tools=[{"type": "function", "function": {"name": "f"}}],
        max_tokens=5000,
    )
    needs = needs_of(request)
    assert needs.vision and needs.tools and needs.tokens > 5000


@pytest.mark.asyncio
async def test_rank_puts_the_best_quality_first_across_providers(redis):
    await _key(redis, Provider.GROQ, ["llama-3.1-8b-instant"], "k1")
    await _key(redis, Provider.OPENAI, ["gpt-4o-mini", "gpt-4o"], "k2")
    await _key(redis, Provider.ANTHROPIC, ["claude-sonnet-4-5"], "k3")

    ranked = await rank_models(PoolRegistry(redis), _SMALL, limit=10)

    assert ranked == ["claude-sonnet-4-5", "gpt-4o", "gpt-4o-mini", "llama-3.1-8b-instant"]


@pytest.mark.asyncio
async def test_rank_drops_models_that_cannot_take_the_request(redis):
    await _key(redis, Provider.GROQ, ["deepseek-r1-distill"], "k1")  # no vision, no tools
    await _key(redis, Provider.OPENAI, ["gpt-4o-mini"], "k2")
    await _key(redis, Provider.MISTRAL, ["mistral-small-latest"], "k3")  # 32k window

    vision = await rank_models(PoolRegistry(redis), Needs(100, vision=True, tools=False), limit=10)
    tools = await rank_models(PoolRegistry(redis), Needs(100, vision=False, tools=True), limit=10)
    long = await rank_models(PoolRegistry(redis), Needs(100_000, vision=False, tools=False), limit=10)

    assert "deepseek-r1-distill" not in vision and "deepseek-r1-distill" not in tools
    assert "mistral-small-latest" not in long and "gpt-4o-mini" in long


@pytest.mark.asyncio
async def test_rank_skips_models_with_no_key_ready_and_suspended_keys(redis):
    best = await _key(redis, Provider.ANTHROPIC, ["claude-opus-4-1"], "k1")
    await _key(redis, Provider.OPENAI, ["gpt-4o"], "k2")
    registry = PoolRegistry(redis)
    await registry.mark_exhausted(best.id, seconds=600, reason="test")

    assert await rank_models(registry, _SMALL, limit=10) == ["gpt-4o"]


@pytest.mark.asyncio
async def test_operator_quality_override_wins(redis, monkeypatch):
    await _key(redis, Provider.OPENAI, ["gpt-4o", "my-finetune"], "k1")
    monkeypatch.setattr(get_settings(), "supermode_quality", {"my-finetune": 99})

    ranked = await rank_models(PoolRegistry(redis), _SMALL, limit=10)

    assert ranked[0] == "my-finetune"


@pytest.mark.asyncio
async def test_chat_is_served_by_the_best_model(client, redis, monkeypatch):
    await _key(redis, Provider.OPENAI, ["gpt-4o-mini"], "k1")
    await _key(redis, Provider.GOOGLE, ["gemini-2.5-pro"], "k2")
    called: list[str] = []

    async def fake_attempt(reg, key_id, model, messages, **params):
        called.append(model)
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    resp = await _chat(client, _hi())

    assert resp.status_code == 200
    assert called == ["gemini-2.5-pro"]
    assert resp.json()["model"] == "gemini-2.5-pro"
    assert resp.headers["X-Palimpsest-Model"] == "gemini-2.5-pro"


@pytest.mark.asyncio
async def test_chat_falls_back_to_the_next_model_when_the_first_is_spent(client, redis, monkeypatch):
    best = await _key(redis, Provider.ANTHROPIC, ["claude-opus-4-1"], "k1")
    await _key(redis, Provider.OPENAI, ["gpt-4o"], "k2")
    called: list[str] = []

    async def fake_attempt(reg, key_id, model, messages, **params):
        called.append(model)
        if key_id == best.id:
            error = ProviderAttemptError(402, "x", "payment required", Provider.ANTHROPIC)
            return AttemptResult(ok=False, error=error)
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    resp = await _chat(client, _hi())

    assert resp.status_code == 200
    assert called == ["claude-opus-4-1", "gpt-4o"]
    assert resp.json()["model"] == "gpt-4o"


@pytest.mark.asyncio
async def test_master_model_still_wins_over_supermode(client, redis, monkeypatch):
    await _key(redis, Provider.OPENAI, ["gpt-4o-mini", "gpt-4o"], "k1")
    monkeypatch.setattr(get_settings(), "master_model", "gpt-4o-mini")
    called: list[str] = []

    async def fake_attempt(reg, key_id, model, messages, **params):
        called.append(model)
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    resp = await _chat(client, _hi())

    assert resp.status_code == 200 and called == ["gpt-4o-mini"]


@pytest.mark.asyncio
async def test_empty_pool_refuses_supermode(client, redis):
    resp = await _chat(client, _hi())

    assert resp.status_code in (429, 503)


@pytest.mark.asyncio
async def test_models_share_one_deadline_so_a_request_cannot_hold_the_session_for_minutes(
    client, redis, monkeypatch
):
    """Each model got its own 2 x provider_timeout bound: three slow ones held the lock for minutes."""
    import time

    await _key(redis, Provider.OPENAI, ["gpt-4o", "gpt-4.1", "gpt-4o-mini"], "o")
    monkeypatch.setattr(get_settings(), "provider_timeout", 1.0)
    deadlines: list[float] = []

    async def fake(reg, key_id, model, messages, **params):
        return AttemptResult(ok=False, error=ProviderAttemptError(503, "x", "upstream down", Provider.OPENAI))

    real = failover_module.execute_with_failover

    async def spy(*args, deadline=None, **kwargs):
        deadlines.append(deadline)
        return await real(*args, deadline=deadline, **kwargs)

    import app.api.chat as chat_module

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    monkeypatch.setattr(chat_module, "execute_with_failover", spy)

    started = time.monotonic()
    resp = await _chat(client, {"model": "palimpsest/supermode", "messages": [{"role": "user", "content": "hi"}]})

    assert resp.status_code in (429, 503)
    assert len(deadlines) >= 2 and len(set(deadlines)) == 1  # one deadline for every model
    assert time.monotonic() - started < 2 * 1.0 + 1.5


def test_broad_family_rows_do_not_guess_a_small_context_window():
    """'llama-3.2-90b-vision-preview' (128k) used to fall to a plain 8k 'llama' row."""
    assert profile_for("llama-3.3-70b-versatile").context == 128_000
    vision = profile_for("llama-3.2-90b-vision-preview")
    assert vision.vision is True and vision.context == 128_000
    for unknown in ("llama-9-future", "gemma-7-unknown", "deepseek-newest"):
        assert profile_for(unknown).context is None  # unknown is never filtered on


@pytest.mark.asyncio
async def test_the_operator_can_set_a_models_context_and_capabilities(redis, monkeypatch):
    await _key(redis, Provider.GROQ, ["house-model", "gpt-4o"], "k")
    registry = PoolRegistry(redis)
    big = Needs(tokens=60_000, vision=True, tools=False)

    # Unknown: the model has no known context and no vision, so an image request drops it.
    assert "house-model" not in await rank_models(registry, big, limit=10)

    monkeypatch.setattr(
        get_settings(), "supermode_profiles", {"house-model": {"context": 200_000, "vision": True}}
    )
    assert "house-model" in await rank_models(registry, big, limit=10)

    monkeypatch.setattr(get_settings(), "supermode_profiles", {"house-model": {"context": 10_000, "vision": True}})
    assert "house-model" not in await rank_models(registry, big, limit=10)


def test_supermode_profiles_are_checked():
    from pydantic import ValidationError

    from app.config import Settings

    assert Settings(supermode_profiles={"m": {"context": 8000, "vision": False, "tools": None}})
    for bad in ({"m": {"speed": 1}}, {"m": {"context": 0}}, {"m": {"vision": 3}}, {"m": {"tools": 3}}):
        with pytest.raises(ValidationError):
            Settings(supermode_profiles=bad)
