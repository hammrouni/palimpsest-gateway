"""A 429 pauses one model on a key, not the whole key; exhaustion reports the real wait."""
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis.aioredis import FakeRedis

import app.router.failover as failover_module
from app.pool.models import KeyCreate, KeyState, KeyUpdate, Provider
from app.pool.registry import PoolRegistry, _model_state_key
from app.provider.adapter import AttemptResult, ProviderAttemptError
from app.router.exhaustion import PoolExhaustedError, render_exhausted
from app.router.failover import execute_with_failover

MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    import os

    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("PALIMPSEST_GATE_MASTER_KEY", Fernet.generate_key().decode())


@pytest.fixture
async def registry():
    return PoolRegistry(FakeRedis())


async def _key(registry, models, material="sk-a"):
    return (
        await registry.create(KeyCreate(provider=Provider.OPENROUTER, models=models, key=material))
    ).id


def _rate_limited():
    return AttemptResult(
        ok=False, error=ProviderAttemptError(429, "rate_limit", "slow", Provider.OPENROUTER)
    )


@pytest.mark.asyncio
async def test_cooldown_on_one_model_leaves_the_keys_other_models_routable(registry):
    key_id = await _key(registry, ["slow-model", "fast-model"])

    await registry.mark_cooldown(key_id, model="slow-model")

    assert await registry.routable_keys(model="slow-model") == []
    assert [k.id for k in await registry.routable_keys(model="fast-model")] == [key_id]
    assert await registry.effective_state(key_id) == KeyState.ACTIVE


@pytest.mark.asyncio
async def test_a_keywide_cooldown_still_pauses_everything(registry):
    key_id = await _key(registry, ["a", "b"])

    await registry.mark_cooldown(key_id)

    assert await registry.routable_keys(model="a") == []
    assert await registry.routable_keys(model="b") == []


@pytest.mark.asyncio
async def test_state_view_lists_cooling_models(registry):
    key_id = await _key(registry, ["slow-model", "fast-model"])
    await registry.mark_cooldown(key_id, model="slow-model")

    (view,) = await registry.list_states()

    assert view.state == KeyState.ACTIVE
    assert [m["model"] for m in view.cooling_models] == ["slow-model"]
    assert 0 < view.cooling_models[0]["seconds_remaining"] <= 60


@pytest.mark.asyncio
async def test_removing_a_key_clears_its_model_cooldowns(registry):
    key_id = await _key(registry, ["slow-model"])
    await registry.mark_cooldown(key_id, model="slow-model")

    await registry.remove(key_id)

    assert [k async for k in registry.redis.scan_iter(match=f"*{key_id}*")] == []


@pytest.mark.asyncio
async def test_all_keys_rate_limited_reports_the_cooldown_end_not_midnight(registry, monkeypatch):
    await _key(registry, ["slow-model"], "sk-a")
    await _key(registry, ["slow-model"], "sk-b")

    async def fake_attempt(reg, key_id, model, messages, **params):
        return _rate_limited()

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    with pytest.raises(PoolExhaustedError) as err:
        await execute_with_failover(registry, "slow-model", MESSAGES)

    wait = (err.value.next_reset - datetime.now(UTC)).total_seconds()
    assert 0 < wait <= 60  # the cooldown, not the quota window (hours away)

    rendered = render_exhausted(err.value, "slow-model")
    assert "Retry in about" in rendered.message
    assert 1 <= int(rendered.headers["Retry-After"]) <= 60
    assert "X-Palimpsest-Next-Reset" in rendered.headers


@pytest.mark.asyncio
async def test_other_model_on_the_same_key_still_works_after_a_429(registry, monkeypatch):
    await _key(registry, ["slow-model", "fast-model"])
    from tests.conftest import _FakeBlockingResponse

    async def fake_attempt(reg, key_id, model, messages, **params):
        if model == "slow-model":
            return _rate_limited()
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    with pytest.raises(PoolExhaustedError):
        await execute_with_failover(registry, "slow-model", MESSAGES)
    result, _trace = await execute_with_failover(registry, "fast-model", MESSAGES)

    assert result.ok


def test_long_waits_keep_the_quota_reset_wording():
    far = PoolExhaustedError("pool_exhausted", datetime.now(UTC) + timedelta(hours=6))
    near = PoolExhaustedError("pool_exhausted", datetime.now(UTC) + timedelta(seconds=45))

    far_msg = render_exhausted(far, "m")
    near_msg = render_exhausted(near, "m")

    assert far_msg.message.endswith("Retry in about 6 hours.")
    assert int(far_msg.headers["Retry-After"]) > 3600
    assert "Retry in about" in near_msg.message
    assert render_exhausted(PoolExhaustedError("pool_exhausted", None), "m").headers.get("Retry-After") is None


@pytest.mark.asyncio
async def test_pause_reason_is_stored_and_listed(registry):
    key_id = await _key(registry, ["m1", "m2"])
    await registry.mark_cooldown(key_id, model="m1", seconds=3600, reason="daily_limit")

    (view,) = await registry.list_states()

    assert view.cooling_models[0]["model"] == "m1"
    assert view.cooling_models[0]["reason"] == "daily_limit"

    await registry.mark_exhausted(key_id, seconds=7200, reason="no_credits")
    (view,) = await registry.list_states()

    assert view.state == KeyState.EXHAUSTED
    assert view.reason == "no_credits"
    assert 7000 < view.seconds_remaining <= 7200


@pytest.mark.asyncio
async def test_a_404_pauses_the_model_on_every_key_of_the_provider(registry, monkeypatch):
    first = await _key(registry, ["gone"], "sk-a")
    second = await _key(registry, ["gone"], "sk-b")
    calls = []

    async def fake_attempt(reg, key_id, model, messages, **params):
        calls.append(key_id)
        return AttemptResult(
            ok=False, error=ProviderAttemptError(404, "not_found", "no such model", Provider.OPENROUTER)
        )

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    with pytest.raises(PoolExhaustedError):
        await execute_with_failover(registry, "gone", MESSAGES)

    assert len(calls) == 1  # one probe learned it for both keys
    for key_id in (first, second):
        assert await registry.routable_keys(model="gone") == []
        assert await registry.effective_state(key_id) == KeyState.ACTIVE


@pytest.mark.asyncio
async def test_a_dns_failure_pauses_neither_key_nor_model(registry, monkeypatch):
    key_id = await _key(registry, ["m"])

    async def fake_attempt(reg, key_id_, model, messages, **params):
        return AttemptResult(
            ok=False,
            error=ProviderAttemptError(
                500, "api_error", "Cannot connect to host openrouter.ai:443 ssl:default [Name or service not known]",
                Provider.OPENROUTER,
            ),
        )

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    with pytest.raises(PoolExhaustedError):
        await execute_with_failover(registry, "m", MESSAGES)

    assert [k.id for k in await registry.routable_keys(model="m")] == [key_id]
    (view,) = await registry.list_states()
    assert view.cooling_models == []


@pytest.mark.asyncio
async def test_share_models_by_provider_lets_every_key_serve_the_union(registry, monkeypatch):
    from app.config import get_settings

    first = await _key(registry, ["a"], "sk-a")
    second = await _key(registry, ["b"], "sk-b")

    assert [k.id for k in await registry.routable_keys(model="a")] == [first]

    monkeypatch.setattr(get_settings(), "share_models_by_provider", True)

    assert {k.id for k in await registry.routable_keys(model="a")} == {first, second}
    assert {k.id for k in await registry.routable_keys(model="b")} == {first, second}
    # stored per-key lists are untouched
    assert {r.id: r.models for r in await registry.list()} == {first: ["a"], second: ["b"]}


@pytest.mark.asyncio
async def test_share_models_ignores_other_providers_and_suspended_keys(registry, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "share_models_by_provider", True)
    a = await _key(registry, ["a"], "sk-a")
    other = (await registry.create(KeyCreate(provider=Provider.GROQ, models=["g"], key="sk-g"))).id
    gone = await _key(registry, ["secret"], "sk-s")
    await registry.suspend(gone, operator_id="op")

    assert await registry.routable_keys(model="g") == [
        k for k in await registry.routable_keys(model="g") if k.id == other
    ]
    assert [k.id for k in await registry.routable_keys(model="g")] == [other]
    assert {k.id for k in await registry.routable_keys(model="secret")} == set()
    assert a in {k.id for k in await registry.routable_keys(model="a")}


@pytest.mark.asyncio
async def test_removed_models_leave_the_paused_list_and_their_pause_is_deleted(registry):
    key = await registry.create(KeyCreate(provider=Provider.OPENROUTER, models=["a", "b"], key="sk-1"))
    await registry.mark_cooldown(key.id, model="a", seconds=600, reason="model_unavailable")
    await registry.mark_cooldown(key.id, model="b", seconds=600, reason="model_unavailable")

    await registry.update(key.id, KeyUpdate(models=["b"]))

    view = next(v for v in await registry.list_states() if v.id == key.id)
    assert [c["model"] for c in view.cooling_models] == ["b"]
    assert await registry.redis.exists(_model_state_key(key.id, "a")) == 0
