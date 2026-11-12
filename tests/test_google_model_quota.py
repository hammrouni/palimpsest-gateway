"""Google's per-model quotas pause that model on the key, not the whole key."""
import pytest
from fakeredis.aioredis import FakeRedis

import app.router.failover as failover_module
from app.pool.models import KeyCreate, KeyState, Provider
from app.pool.registry import PoolRegistry, _model_state_key
from app.provider.adapter import AttemptResult, ProviderAttemptError
from app.router.classify import classify, model_quota_limit, pause_reason
from app.router.exhaustion import PoolExhaustedError
from app.router.failover import execute_with_failover
from tests.conftest import _FakeBlockingResponse

# What litellm hands back for a free-tier project asking for Gemini Pro.
_ZERO = (
    'litellm.RateLimitError: geminiException - { "error": { "code": 429, "message": '
    '"You exceeded your current quota, please check your plan and billing details. '
    "\\n* Quota exceeded for metric: generativelanguage.googleapis.com/"
    "generate_content_free_tier_input_token_count, limit: 0, model: gemini-3.1-pro, "
    'description: Generate content input tokens per day limit exceeded on Free Tier", '
    '"details": [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "47025s"}]}}'
)
_SPENT = _ZERO.replace("limit: 0", "limit: 20").replace("per day", "per minute").replace(
    "47025s", "31s"
)


def _err(message: str, status: int = 429) -> ProviderAttemptError:
    return ProviderAttemptError(status, "rate_limit", message, Provider.GOOGLE)


def test_a_zero_model_quota_is_model_scoped_and_reads_as_unavailable():
    error = _err(_ZERO)
    assert classify(error).state == "model_scoped"
    assert model_quota_limit(error) == 0
    assert pause_reason(error) == "model_unavailable"


def test_a_spent_model_quota_is_model_scoped_and_reads_as_a_rate_limit():
    error = _err(_SPENT)
    assert classify(error).state == "model_scoped"
    assert model_quota_limit(error) == 20
    assert pause_reason(error) == "rate_limit"


def test_an_account_wide_quota_still_pauses_the_whole_key():
    error = _err("You exceeded your current quota, please check your plan and billing details.")
    assert classify(error).state == "exhausted"
    assert model_quota_limit(error) is None


@pytest.fixture
async def registry():
    return PoolRegistry(FakeRedis())


async def _run(registry, monkeypatch, message, models=("gemini-3.1-pro-preview", "gemini-3-flash")):
    key = await registry.create(KeyCreate(provider=Provider.GOOGLE, models=list(models), key="g"))

    async def fake(reg, key_id, model, messages, **params):
        if model == "gemini-3.1-pro-preview":
            return AttemptResult(ok=False, error=_err(message))
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    with pytest.raises(PoolExhaustedError):
        await execute_with_failover(
            registry, "gemini-3.1-pro-preview", [{"role": "user", "content": "hi"}]
        )
    return key


@pytest.mark.asyncio
async def test_a_zero_quota_pauses_only_that_model_for_a_day(registry, monkeypatch):
    key = await _run(registry, monkeypatch, _ZERO)

    record = await registry.get(key.id)
    assert record.state == KeyState.ACTIVE  # the key's other models keep working
    ttl = await registry.redis.ttl(_model_state_key(key.id, "gemini-3.1-pro-preview"))
    assert 86000 < ttl <= 86400
    result, _ = await execute_with_failover(
        registry, "gemini-3-flash", [{"role": "user", "content": "hi"}]
    )
    assert result.ok


@pytest.mark.asyncio
async def test_a_spent_quota_pauses_the_model_until_the_provider_says(registry, monkeypatch):
    key = await _run(registry, monkeypatch, _SPENT)

    ttl = await registry.redis.ttl(_model_state_key(key.id, "gemini-3.1-pro-preview"))
    assert 0 < ttl <= 31


def test_the_key_test_reports_a_zero_model_quota_as_needing_a_paid_plan():
    from app.pool.probe import _verdict

    assert _verdict(_err(_ZERO)) == "no_credits"
    assert _verdict(_err(_SPENT)) == "rate_limited"


class _FailingStream:
    def __init__(self, message):
        self.message = message

    def __aiter__(self):
        return self

    async def __anext__(self):
        error = Exception(self.message)
        error.status_code = 429
        error.message = self.message
        raise error


@pytest.mark.asyncio
async def test_a_quota_error_at_stream_start_keeps_the_model_detail(registry, monkeypatch):
    """litellm reports a stream that fails on its first read with a long prefix."""
    advice = (
        "For more information on this error, head to: https://aistudio.google.com/docs/"
        "rate-limits. To monitor your current usage, head to: https://ai.dev/rate-limit. "
    )
    message = (
        "litellm.MidStreamFallbackError: litellm.RateLimitError: litellm.RateLimitError: "
        + _SPENT.replace("billing details. ", "billing details. " + advice)
    )
    assert message.index("limit: 20") > 500  # past where it used to be cut
    key = await registry.create(
        KeyCreate(provider=Provider.GOOGLE, models=["gemini-3-flash-preview"], key="gs")
    )

    async def fake(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=_FailingStream(message))

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    with pytest.raises(PoolExhaustedError):
        await execute_with_failover(
            registry, "gemini-3-flash-preview", [{"role": "user", "content": "hi"}], stream=True
        )

    assert (await registry.get(key.id)).state == KeyState.ACTIVE
    ttl = await registry.redis.ttl(_model_state_key(key.id, "gemini-3-flash-preview"))
    assert 0 < ttl <= 31
