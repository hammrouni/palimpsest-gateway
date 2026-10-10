"""A 429 pauses one model on a key, not the whole key; exhaustion reports the real wait."""
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis.aioredis import FakeRedis

import app.router.failover as failover_module
from app.pool.models import KeyCreate, KeyState, Provider
from app.pool.registry import PoolRegistry
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
