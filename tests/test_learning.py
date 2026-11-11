"""Auto-improve: what failures and successes teach the gateway."""
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

import app.router.failover as failover_module
from app.config import get_settings
from app.main import app
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.provider.adapter import AttemptResult, ProviderAttemptError
from app.router.learning import (
    Learned,
    Needs,
    capability_refused,
    keys_at_learned_limit,
    learned_many,
    note_attempt,
    record_failure,
    record_success,
)
from app.router.selection import select_candidates
from app.router.supermode import rank_models
from app.store.redis_store import RedisStore
from tests.conftest import _FakeBlockingResponse

_PLAIN = Needs(tokens=500, vision=False, tools=False)


def _err(status, message, provider=Provider.OPENAI):
    return ProviderAttemptError(status, "x", message, provider)


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture(autouse=True)
def _no_masters(monkeypatch):
    monkeypatch.setattr(get_settings(), "master_model", None)
    monkeypatch.setattr(get_settings(), "provider_master_models", {})
    monkeypatch.setattr(get_settings(), "supermode_quality", {})


def test_capability_refusals_are_recognised():
    assert capability_refused(_err(400, "maximum context length is 8192 tokens")) == "context"
    assert capability_refused(_err(400, "This model does not support image input")) == "vision"
    assert capability_refused(_err(400, "tool calling is not supported for this model")) == "tools"
    assert capability_refused(_err(400, "bad temperature")) is None
    assert capability_refused(_err(429, "slow down")) is None


def test_learned_context_is_capped_by_failures_and_raised_by_successes():
    assert Learned(ctx_fail=9000).context(128_000) == 8999
    assert Learned(ctx_ok=300_000).context(128_000) == 300_000
    assert Learned().context(None) is None
    assert Learned(ctx_fail=5000).context(None) == 4999


def test_penalty_needs_enough_history_and_counts_recent_throttles():
    assert Learned(ok=1, fail=2).penalty() == 0  # too few to judge
    assert Learned(ok=5, fail=5).penalty() == 10
    assert Learned(throttled_at=1000.0).penalty(now=1010.0) == 5
    assert Learned(throttled_at=1000.0).penalty(now=5000.0) == 0


@pytest.mark.asyncio
async def test_a_context_refusal_then_a_bigger_success_corrects_itself(redis):
    await record_failure(
        redis, "m", "k", Needs(9000, False, False),
        _err(400, "context length exceeded"), "fatal", None, False,
    )
    assert (await learned_many(redis, ["m"]))["m"].ctx_fail == 9000

    await record_success(redis, "m", "k", Needs(12000, False, False))
    learned = (await learned_many(redis, ["m"]))["m"]
    assert learned.ctx_fail is None and learned.ctx_ok == 12000


@pytest.mark.asyncio
async def test_capabilities_are_learned_both_ways(redis):
    await record_success(redis, "seer", "k", Needs(100, vision=True, tools=True))
    await record_failure(
        redis, "blind", "k", Needs(100, vision=True, tools=False),
        _err(400, "does not support image input"), "fatal", None, False,
    )
    learned = await learned_many(redis, ["seer", "blind"])
    assert learned["seer"].vision is True and learned["seer"].tools is True
    assert learned["blind"].vision is False


@pytest.mark.asyncio
async def test_connectivity_failures_teach_nothing(redis):
    await record_failure(
        redis, "m", "k", _PLAIN, _err(None, "connection refused"), "transient", None, True
    )
    assert (await learned_many(redis, ["m"]))["m"] == Learned()


@pytest.mark.asyncio
async def test_a_headerless_429_teaches_the_key_its_rate(redis):
    for _ in range(4):
        await note_attempt(redis, "k1")
    await record_failure(redis, "m", "k1", _PLAIN, _err(429, "too many requests"), "transient", {}, False)

    assert await keys_at_learned_limit(redis, ["k1", "k2"]) == {"k1"}


@pytest.mark.asyncio
async def test_a_429_with_rate_headers_leaves_the_provider_in_charge(redis):
    await note_attempt(redis, "k1")
    await note_attempt(redis, "k1")
    headers = {"x-ratelimit-remaining-requests": "0"}
    await record_failure(redis, "m", "k1", _PLAIN, _err(429, "slow"), "transient", headers, False)

    assert await keys_at_learned_limit(redis, ["k1"]) == set()


@pytest.mark.asyncio
async def test_a_success_past_the_learned_rate_raises_it(redis):
    for _ in range(3):
        await note_attempt(redis, "k1")
    await record_failure(redis, "m", "k1", _PLAIN, _err(429, "slow"), "transient", None, False)
    await note_attempt(redis, "k1")  # 4 sent, limit learned as 2
    await record_success(redis, "m", "k1", _PLAIN)

    assert await redis.get("pgw:learn:key:k1:rpm") == b"4"


@pytest.mark.asyncio
async def test_keys_at_their_learned_rate_are_tried_last(redis):
    registry = PoolRegistry(redis)
    a = await registry.create(KeyCreate(provider=Provider.GROQ, models=["m"], key="ka"))
    b = await registry.create(KeyCreate(provider=Provider.GROQ, models=["m"], key="kb"))
    first = (await select_candidates(registry, "m"))[0].key_id
    second = b.id if first == a.id else a.id

    for _ in range(3):
        await note_attempt(redis, first)
    await record_failure(redis, "m", first, _PLAIN, _err(429, "slow"), "transient", None, False)

    order = [c.key_id for c in await select_candidates(registry, "m")]
    assert order == [second, first]


@pytest.mark.asyncio
async def test_supermode_skips_a_model_that_learned_it_cannot_see(redis):
    registry = PoolRegistry(redis)
    await registry.create(KeyCreate(provider=Provider.OPENAI, models=["gpt-4o", "gpt-4o-mini"], key="k"))
    image = Needs(100, vision=True, tools=False)
    assert (await rank_models(registry, image, limit=5))[0] == "gpt-4o"

    await record_failure(
        redis, "gpt-4o", "k", image, _err(400, "does not support image input"), "fatal", None, False
    )

    assert await rank_models(registry, image, limit=5) == ["gpt-4o-mini"]


@pytest.mark.asyncio
async def test_supermode_ranks_an_unreliable_model_lower(redis):
    registry = PoolRegistry(redis)
    await registry.create(KeyCreate(provider=Provider.OPENAI, models=["gpt-4o", "gpt-4.1"], key="k"))
    assert (await rank_models(registry, _PLAIN, limit=5))[0] == "gpt-4.1"

    for _ in range(8):
        await record_failure(redis, "gpt-4.1", "k", _PLAIN, _err(503, "upstream"), "transient", None, False)

    assert (await rank_models(registry, _PLAIN, limit=5))[0] == "gpt-4o"


@pytest.mark.asyncio
async def test_chat_moves_to_the_next_model_when_one_refuses_images(redis, monkeypatch):
    registry = PoolRegistry(redis)
    await registry.create(KeyCreate(provider=Provider.OPENAI, models=["gpt-4o", "gpt-4o-mini"], key="k"))
    called: list[str] = []

    async def fake_attempt(reg, key_id, model, messages, **params):
        called.append(model)
        if model == "gpt-4o":
            return AttemptResult(ok=False, error=_err(400, "does not support image input"))
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        account = await client.post("/v1/accounts", json={})
        api_key = account.json()["keys"][0]["key"]
        content = [
            {"type": "text", "text": "what is this"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
        ]
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "palimpsest/supermode", "messages": [{"role": "user", "content": content}]},
        )

    assert resp.status_code == 200
    assert called == ["gpt-4o", "gpt-4o-mini"]
    assert (await learned_many(redis, ["gpt-4o"]))["gpt-4o"].vision is False


def test_an_image_counts_as_a_flat_cost_not_its_base64_length():
    from app.router.learning import needs_from

    huge = "data:image/png;base64," + "A" * 2_000_000
    needs = needs_from(
        [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": huge}}]}]
    )
    assert needs.vision and needs.tokens < 5000
