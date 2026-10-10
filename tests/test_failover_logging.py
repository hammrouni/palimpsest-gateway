"""Each failed provider attempt is logged with the provider's reason, minus any key."""
import pytest
from fakeredis.aioredis import FakeRedis

import app.router.failover as failover_module
from app.core.redact import redact
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.provider.adapter import AttemptResult, ProviderAttemptError
from app.router.exhaustion import PoolExhaustedError
from app.router.failover import execute_with_failover


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    import os

    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("PALIMPSEST_GATE_MASTER_KEY", Fernet.generate_key().decode())


@pytest.mark.parametrize(
    "text",
    [
        "bad key sk-live-abcdef123456 given",
        "bad key sk-or-v1-0123456789abcdef given",
        "key=AIzaSyA1234567890abcdefghijklmnop rejected",
        "gsk_abcdefghijklmnop failed",
        "pgw_opk_abcdefgh12345678 leaked",
    ],
)
def test_key_shaped_text_is_removed(text):
    cleaned = redact(text)

    assert "[REDACTED]" in cleaned
    for secret in ("abcdef123456", "0123456789abcdef", "A1234567890", "abcdefghijklmnop", "abcdefgh12345678"):
        assert secret not in cleaned


def test_ordinary_error_text_is_left_alone():
    message = "Quota exceeded for metric generate_content_free_tier_requests, limit: 5"

    assert redact(message) == message


@pytest.mark.asyncio
async def test_a_failed_attempt_logs_the_providers_reason(monkeypatch, caplog):
    registry = PoolRegistry(FakeRedis())
    await registry.create(KeyCreate(provider=Provider.GOOGLE, models=["m"], key="g-key-a"))

    async def fake_attempt(reg, key_id, model, messages, **params):
        reason = (
            "Quota exceeded for metric generate_content_free_tier_input_token_count, "
            "limit: 250000, key AIzaSyA1234567890abcdefghijklmnop"
        )
        return AttemptResult(ok=False, error=ProviderAttemptError(429, "rate_limit", reason, Provider.GOOGLE))

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    with caplog.at_level("WARNING", logger="palimpsest.failover"), pytest.raises(PoolExhaustedError):
        await execute_with_failover(registry, "m", [{"role": "user", "content": "x"}])

    assert "provider=google" in caplog.text and "status=429" in caplog.text
    assert "generate_content_free_tier_input_token_count" in caplog.text  # which limit
    assert "AIzaSyA1234567890" not in caplog.text  # the key is not
