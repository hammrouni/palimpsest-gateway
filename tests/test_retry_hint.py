"""A provider's own "retry in ..." decides how long a key is paused."""
from datetime import UTC, datetime

import pytest
from fakeredis.aioredis import FakeRedis

import app.router.failover as failover_module
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.provider.adapter import AttemptResult, ProviderAttemptError
from app.router.exhaustion import PoolExhaustedError, render_exhausted
from app.router.failover import execute_with_failover
from app.router.retry_hint import retry_after_seconds

GOOGLE_DAILY = (
    '{ "error": { "code": 429, "message": "You exceeded your current quota. * Quota exceeded for '
    "metric: generate_content_free_tier_requests, limit: 20, model: gemini-3.8-flash\\n"
    'Please retry in 7h14m58.686490024s.", "status": "RESOURCE_EXHAUSTED" } }'
)


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    import os

    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("PALIMPSEST_GATE_MASTER_KEY", Fernet.generate_key().decode())


@pytest.mark.parametrize(
    ("message", "headers", "expected"),
    [
        (GOOGLE_DAILY, None, 7 * 3600 + 14 * 60 + 59),  # rounded up to whole seconds
        ("Please retry in 37s", None, 37),
        ("retry in 2m", None, 120),
        ("retry in 1d", None, 86400),
        ('{"details":[{"retryDelay":"12s"}]}', None, 12),
        ("rate limited", {"retry-after": "30"}, 30),
        ("rate limited", {"llm_provider-retry-after": "45"}, 45),
        ("Please retry in 10s", {"Retry-After": "20"}, 20),  # the header wins
        ("retry in 99d", None, 86400),  # never more than a day
        ("rate limited", None, None),
        ("rate limited", {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"}, None),
        ("retry in 0s", None, None),
        (None, None, None),
        ("", {}, None),
    ],
)
def test_the_wait_a_provider_asks_for(message, headers, expected):
    assert retry_after_seconds(message, headers) == expected


@pytest.mark.asyncio
async def test_a_quota_429_pauses_the_key_for_the_time_google_names(monkeypatch):
    registry = PoolRegistry(FakeRedis())
    await registry.create(KeyCreate(provider=Provider.GOOGLE, models=["m"], key="g-daily"))

    async def fake_attempt(reg, key_id, model, messages, **params):
        error = ProviderAttemptError(429, "rate_limit", GOOGLE_DAILY, Provider.GOOGLE)
        return AttemptResult(ok=False, error=error)

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    with pytest.raises(PoolExhaustedError) as err:
        await execute_with_failover(registry, "m", [{"role": "user", "content": "x"}])

    wait = (err.value.next_reset - datetime.now(UTC)).total_seconds()
    assert 7 * 3600 + 14 * 60 < wait <= 7 * 3600 + 15 * 60  # about 7h15m, not 60 s

    rendered = render_exhausted(err.value, "m")
    assert "Retry in about 7 hours." in rendered.message
    assert 26000 < int(rendered.headers["Retry-After"]) <= 26100
    # And the key is not retried in the meantime.
    assert await registry.routable_keys(model="m") == []


@pytest.mark.asyncio
async def test_without_a_hint_the_default_pause_still_applies(monkeypatch):
    registry = PoolRegistry(FakeRedis())
    await registry.create(KeyCreate(provider=Provider.GOOGLE, models=["m"], key="g-nohint"))

    async def fake_attempt(reg, key_id, model, messages, **params):
        error = ProviderAttemptError(429, "rate_limit", "slow down", Provider.GOOGLE)
        return AttemptResult(ok=False, error=error)

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    with pytest.raises(PoolExhaustedError) as err:
        await execute_with_failover(registry, "m", [{"role": "user", "content": "x"}])

    assert 0 < (err.value.next_reset - datetime.now(UTC)).total_seconds() <= 60


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [
        (45, "Retry in about 45 seconds."),
        (600, "Retry in about 600 seconds."),
        (1500, "Retry in about 25 minutes."),
        (7 * 3600 + 900, "Retry in about 7 hours."),
        (3600, "Retry in about 1 hour."),
        (3 * 86400, "Retry in about 3 days."),
    ],
)
def test_waits_are_put_in_words(seconds, expected):
    from datetime import timedelta

    # Half a second short, so the wait rounds up to exactly ``seconds`` however slowly this runs.
    error = PoolExhaustedError("pool_exhausted", datetime.now(UTC) + timedelta(seconds=seconds - 0.5))

    assert render_exhausted(error, "m").message.endswith(expected)
