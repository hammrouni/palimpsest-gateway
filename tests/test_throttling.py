"""Rate-limit handling seen from outside: throttled refusals are not errors,
repeats inside a Retry-After window are counted once, an empty pool is refused
early, a budget spent on rate limits is a 429, and a short wait is optional."""
import os
import time
from datetime import UTC, datetime

import pytest
from fakeredis.aioredis import FakeRedis

import app.router.failover as failover_module
from app.config import get_settings
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.provider.adapter import AttemptResult, ProviderAttemptError
from app.router.exhaustion import PoolExhaustedError, render_exhausted
from app.router.failover import (
    AttemptRecord,
    FailoverTrace,
    capacity_refusal,
    execute_with_failover,
)
from app.usage.stats import StatsStore
from app.usage.store import UsageStore

MODEL = "gpt-4o-mini"
MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _master_key():
    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        os.environ["PALIMPSEST_GATE_MASTER_KEY"] = Fernet.generate_key().decode()


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def registry(redis):
    return PoolRegistry(redis)


async def _add_key(registry: PoolRegistry, key: str) -> str:
    record = await registry.create(KeyCreate(provider=Provider.OPENAI, models=[MODEL], key=key))
    return record.id


async def _record(
    store: UsageStore, outcome: str, status: int, account_id: str = "acct", **extra
) -> None:
    await store.record_request(
        request_id=f"r-{time.monotonic_ns()}", account_id=account_id, session_id=None, model=MODEL,
        status_code=status, outcome=outcome, latency_ms=5, stream=False, attempts=0,
        prompt_tokens=None, completion_tokens=None, provider=None, **extra,
    )


# -- 1. throttled is its own count, not an error --------------------------------


@pytest.mark.asyncio
async def test_throttled_refusals_are_not_errors(redis):
    store = UsageStore(redis)
    await store.record_request(
        request_id="ok", account_id="acct", session_id=None, model=MODEL, status_code=200,
        outcome="success", latency_ms=300, stream=False, attempts=1, prompt_tokens=3,
        completion_tokens=2, provider="openai", key_id="k",
    )
    await _record(store, "exhausted_429", 429, retry_after_s=30)
    # Another user: a refusal is only deduplicated per account and model.
    await _record(store, "rejected_user_cap", 429, account_id="other", retry_after_s=30)

    totals = (await StatsStore(redis).daily(1))["totals"]
    assert totals["requests"] == 3
    assert totals["throttled"] == 2
    assert totals["errors_4xx"] == 0 and totals["errors_5xx"] == 0
    # Rates are over handled requests only: the one served request succeeded.
    assert totals["handled"] == 1
    assert totals["success_rate"] == 1.0
    assert totals["error_rate"] == 0.0
    # Instant refusals do not pull the latency down.
    assert totals["avg_latency_ms"] == 300.0
    assert totals["exhausted"] == 1


# -- 2. a refusal repeated inside its Retry-After window counts once ------------


@pytest.mark.asyncio
async def test_repeat_refusals_within_retry_after_count_once(redis):
    store = UsageStore(redis)
    for _ in range(5):
        await _record(store, "exhausted_429", 429, retry_after_s=40)

    totals = (await StatsStore(redis).daily(1))["totals"]
    assert totals["requests"] == 1
    assert totals["throttled"] == 1
    assert totals["repeats"] == 4
    assert len(await store.recent(50)) == 1
    # The marker lasts as long as the client was told to wait.
    ttl = await redis.ttl(f"usage:throttled:acct:{MODEL}")
    assert 0 < ttl <= 40


@pytest.mark.asyncio
async def test_real_errors_are_never_deduplicated(redis):
    store = UsageStore(redis)
    for _ in range(3):
        await _record(store, "error_5xx", 503)
    totals = (await StatsStore(redis).daily(1))["totals"]
    assert totals["requests"] == 3
    assert totals["errors_5xx"] == 3
    assert totals["repeats"] == 0


# -- 3. an empty pool is refused before any work --------------------------------


@pytest.mark.asyncio
async def test_capacity_refusal_when_every_key_is_paused(registry):
    key_id = await _add_key(registry, "sk-a")
    assert await capacity_refusal(registry, MODEL) is None

    await registry.mark_cooldown(key_id, model=MODEL, seconds=120, reason="rate_limited")
    refusal = await capacity_refusal(registry, MODEL)
    assert isinstance(refusal, PoolExhaustedError)
    rendered = render_exhausted(refusal, model=MODEL)
    assert rendered.status_code == 429
    assert 1 <= int(rendered.headers["Retry-After"]) <= 120


@pytest.mark.asyncio
async def test_capacity_refusal_defers_to_a_short_wait(registry, monkeypatch):
    key_id = await _add_key(registry, "sk-a")
    await registry.mark_cooldown(key_id, model=MODEL, seconds=2, reason="rate_limited")
    monkeypatch.setattr(get_settings(), "failover_max_wait_seconds", 5)
    assert await capacity_refusal(registry, MODEL) is None


# -- 4. a retry budget spent only on rate limits is a 429 -----------------------


def _spent(statuses: list[int], classifications: list[str]) -> PoolExhaustedError:
    error = PoolExhaustedError(
        "budget_exhausted", datetime.now(UTC).replace(microsecond=0), retry_count=len(statuses) - 1
    )
    error.trace = FailoverTrace(
        model=MODEL,
        attempts=tuple(
            AttemptRecord(
                key_id=f"k{i}", provider=Provider.OPENAI, classification=cls,
                outcome="failed", status_code=status,
            )
            for i, (status, cls) in enumerate(zip(statuses, classifications, strict=True))
        ),
        terminal="budget_exhausted",
        retry_count=len(statuses) - 1,
    )
    return error


def test_budget_spent_on_rate_limits_is_a_429_naming_the_tries():
    rendered = render_exhausted(_spent([429, 429, 429, 429], ["transient"] * 4), model=MODEL)
    assert rendered.status_code == 429
    assert "All 4 keys tried" in rendered.message
    assert "rate-limited" in rendered.message


def test_budget_spent_on_outages_stays_a_503():
    rendered = render_exhausted(_spent([429, 502], ["transient", "transient"]), model=MODEL)
    assert rendered.status_code == 503


# -- 5. optional short wait for a key that is about to come back ----------------


@pytest.mark.asyncio
async def test_loop_waits_for_a_key_back_within_the_allowance(registry, monkeypatch):
    key_id = await _add_key(registry, "sk-a")
    calls: list[str] = []

    async def fake(registry, key_id, model, messages, **params):
        calls.append(key_id)
        if len(calls) == 1:
            return AttemptResult(
                ok=False, error=ProviderAttemptError(429, "rate", "Retry after 1s", Provider.OPENAI)
            )
        return AttemptResult(ok=True, response=object())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    monkeypatch.setattr(failover_module, "retry_after_seconds", lambda *a, **k: 1)
    monkeypatch.setattr(get_settings(), "failover_max_wait_seconds", 3)

    result, trace = await execute_with_failover(registry, MODEL, MESSAGES)
    assert result.ok
    assert calls == [key_id, key_id]
    assert trace.terminal == "success"


@pytest.mark.asyncio
async def test_loop_does_not_wait_when_off(registry, monkeypatch):
    await _add_key(registry, "sk-a")

    async def fake(registry, key_id, model, messages, **params):
        return AttemptResult(
            ok=False, error=ProviderAttemptError(429, "rate", "slow down", Provider.OPENAI)
        )

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    monkeypatch.setattr(get_settings(), "failover_max_wait_seconds", 0)
    with pytest.raises(PoolExhaustedError) as caught:
        await execute_with_failover(registry, MODEL, MESSAGES)
    rendered = render_exhausted(caught.value, model=MODEL)
    assert rendered.status_code == 429
    assert "All 1 key tried" in rendered.message
