"""A provider timeout is retried on the next key; it does not end the request."""
import logging

import pytest
from fakeredis.aioredis import FakeRedis

import app.router.failover as failover_module
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.provider.adapter import AttemptResult, ProviderAttemptError
from app.router.classify import classify
from app.router.exhaustion import PoolExhaustedError
from app.router.failover import execute_with_failover


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    import os

    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("PALIMPSEST_GATE_MASTER_KEY", Fernet.generate_key().decode())


def _error(status, message="x"):
    return ProviderAttemptError(status, "e", message, Provider.GOOGLE)


@pytest.mark.parametrize("status", [408, 500, 502, 503, 504, None, 429])
def test_timeouts_and_server_errors_are_transient(status):
    assert classify(_error(status)).state == "transient"


@pytest.mark.parametrize("status", [400, 404, 422])
def test_client_errors_stay_fatal_or_invalid(status):
    assert classify(_error(status)).state == "fatal"


@pytest.mark.asyncio
async def test_a_timeout_on_one_key_fails_over_to_the_next(monkeypatch):
    from tests.conftest import _FakeBlockingResponse

    registry = PoolRegistry(FakeRedis())
    for material in ("g-a", "g-b"):
        await registry.create(KeyCreate(provider=Provider.GOOGLE, models=["m"], key=material))
    calls = []

    async def fake_attempt(reg, key_id, model, messages, **params):
        calls.append(key_id)
        if len(calls) == 1:
            return AttemptResult(ok=False, error=_error(408, "Connection timed out"))
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    result, trace = await execute_with_failover(registry, "m", [{"role": "user", "content": "x"}])

    assert result.ok and len(calls) == 2 and trace.attempts[0].classification == "transient"


@pytest.mark.asyncio
async def test_every_key_timing_out_is_a_retryable_503_not_a_502(monkeypatch):
    registry = PoolRegistry(FakeRedis())
    await registry.create(KeyCreate(provider=Provider.GOOGLE, models=["m"], key="g-only"))

    async def fake_attempt(reg, key_id, model, messages, **params):
        return AttemptResult(ok=False, error=_error(408, "Connection timed out"))

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    with pytest.raises(PoolExhaustedError) as err:
        await execute_with_failover(registry, "m", [{"role": "user", "content": "x"}])

    assert err.value.next_reset is not None  # the client is told when to come back


@pytest.mark.asyncio
async def test_multiline_provider_errors_are_logged_on_one_line(monkeypatch, caplog):
    registry = PoolRegistry(FakeRedis())
    await registry.create(KeyCreate(provider=Provider.GOOGLE, models=["m"], key="g-log"))
    reason = '{\n  "error": {\n    "code": 429,\n    "message": "Quota exceeded for metric X",\n    "status": "RESOURCE_EXHAUSTED"\n  }\n}'

    async def fake_attempt(reg, key_id, model, messages, **params):
        return AttemptResult(ok=False, error=_error(429, reason))

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    with caplog.at_level(logging.WARNING, logger="palimpsest.failover"), pytest.raises(PoolExhaustedError):
        await execute_with_failover(registry, "m", [{"role": "user", "content": "x"}])

    line = next(r.getMessage() for r in caplog.records if "provider attempt failed" in r.getMessage())
    assert "\n" not in line
    assert "Quota exceeded for metric X" in line and "RESOURCE_EXHAUSTED" in line
