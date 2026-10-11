"""The single-attempt LiteLLM provider adapter."""
import logging
import os
import pathlib

import pytest
from fakeredis.aioredis import FakeRedis
from litellm.exceptions import (
    BadRequestError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

import app.provider.adapter as adapter_module
from app.pool.models import KeyCreate, Provider
from app.pool.registry import KeyNotFoundError, PoolRegistry
from app.provider.adapter import CandidateGoneError, attempt


@pytest.fixture(autouse=True)
def _master_key():
    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        os.environ["PALIMPSEST_GATE_MASTER_KEY"] = Fernet.generate_key().decode()


@pytest.fixture
async def registry():
    return PoolRegistry(FakeRedis())


class _FakeModelResponse:
    def __init__(self):
        self.id = "chatcmpl-fake"


# -- single call, exact model mapping, api_key, api_base ----------------------


@pytest.mark.asyncio
async def test_attempt_calls_acompletion_exactly_once_with_mapped_model(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    canary = "canary-raw-material-xyz"
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key=canary)
    )

    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return _FakeModelResponse()

    monkeypatch.setattr(adapter_module, "acompletion", fake_acompletion)

    result = await attempt(registry, record.id, "gpt-4o-mini", [{"role": "user", "content": "hi"}])

    assert result.ok is True
    assert len(calls) == 1
    assert calls[0]["model"] == "openai/gpt-4o-mini"
    assert calls[0]["api_key"] == canary
    assert calls[0]["api_base"] is None
    assert calls[0]["messages"] == [{"role": "user", "content": "hi"}]


@pytest.mark.asyncio
async def test_anthropic_call_uses_mapped_model_string(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    record = await registry.create(
        KeyCreate(provider=Provider.ANTHROPIC, models=["claude-x"], key="ant-key")
    )

    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return _FakeModelResponse()

    monkeypatch.setattr(adapter_module, "acompletion", fake_acompletion)

    await attempt(registry, record.id, "claude-x", [{"role": "user", "content": "hi"}])

    assert calls[0]["model"] == "anthropic/claude-x"
    assert calls[0]["api_base"] is None


@pytest.mark.asyncio
async def test_google_call_uses_mapped_model_string(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    record = await registry.create(
        KeyCreate(provider=Provider.GOOGLE, models=["gemini-x"], key="g-key")
    )

    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return _FakeModelResponse()

    monkeypatch.setattr(adapter_module, "acompletion", fake_acompletion)

    await attempt(registry, record.id, "gemini-x", [{"role": "user", "content": "hi"}])

    assert calls[0]["model"] == "gemini/gemini-x"  # litellm has no "google/" route
    assert calls[0]["api_base"] is None


@pytest.mark.asyncio
async def test_provider_params_are_forwarded_to_acompletion(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="param-key")
    )

    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return _FakeModelResponse()

    monkeypatch.setattr(adapter_module, "acompletion", fake_acompletion)

    await attempt(
        registry,
        record.id,
        "gpt-4o-mini",
        [{"role": "user", "content": "hi"}],
        temperature=0.7,
        max_tokens=100,
    )

    assert calls[0]["temperature"] == 0.7
    assert calls[0]["max_tokens"] == 100


@pytest.mark.asyncio
async def test_unrecognized_params_are_filtered(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="filter-key")
    )

    calls = []

    async def fake_acompletion(**kwargs):
        calls.append(kwargs)
        return _FakeModelResponse()

    monkeypatch.setattr(adapter_module, "acompletion", fake_acompletion)

    await attempt(
        registry,
        record.id,
        "gpt-4o-mini",
        [{"role": "user", "content": "hi"}],
        temperature=0.7,
        bogus_param="should_not_reach",
    )

    assert "bogus_param" not in calls[0]
    assert calls[0]["temperature"] == 0.7


def test_no_litellm_router_or_forbidden_retry_kwargs_in_app():
    import ast

    app_dir = pathlib.Path("app")
    forbidden_kwargs = {"num_retries", "fallbacks", "allowed_fails"}

    for path in app_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "litellm.router", f"{path} imports litellm.router"
                    assert not (alias.name.startswith("litellm") and "router" in alias.name), (
                        f"{path} imports litellm router: {alias.name}"
                    )
            if isinstance(node, ast.ImportFrom):
                if node.module and "litellm" in node.module:
                    for alias in node.names:
                        assert alias.name != "Router", f"{path} imports litellm.Router"
            if isinstance(node, ast.Call):
                for kw in node.keywords:
                    assert kw.arg not in forbidden_kwargs, (
                        f"{path} uses forbidden kwarg {kw.arg!r}"
                    )


@pytest.mark.asyncio
async def test_no_litellm_verbosity_emission_in_adapter(monkeypatch):
    import litellm

    assert litellm.verbose is False
    assert litellm.logging is False


# -- provider error scrubbed; network error never escapes --------------------


@pytest.mark.asyncio
async def test_generic_network_error_never_escapes(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="net-key")
    )

    async def failing_acompletion(**kwargs):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(adapter_module, "acompletion", failing_acompletion)

    result = await attempt(registry, record.id, "gpt-4o-mini", [{"role": "user", "content": "hi"}])

    assert result.ok is False
    assert result.error.status_code is None


@pytest.mark.asyncio
async def test_rate_limit_error_scrubbed_and_mapped(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch, caplog
):
    caplog.set_level(logging.DEBUG)
    canary = "canary-secret-in-error-body"
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key=canary)
    )

    async def failing_acompletion(**kwargs):
        raise RateLimitError(
            message=f"rate limited, offending key={canary}",
            llm_provider="openai",
            model="gpt-4o-mini",
        )

    monkeypatch.setattr(adapter_module, "acompletion", failing_acompletion)

    result = await attempt(registry, record.id, "gpt-4o-mini", [{"role": "user", "content": "hi"}])

    assert result.ok is False
    assert result.error.status_code == 429
    assert canary not in str(result.error)
    assert canary not in result.error.message
    assert canary not in caplog.text


@pytest.mark.asyncio
async def test_bad_request_error_scrubbed_and_mapped(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    canary = "sk-test-bad-request-key"
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key=canary)
    )

    async def failing_acompletion(**kwargs):
        raise BadRequestError(
            message=f"bad request, key={canary}",
            llm_provider="openai",
            model="gpt-4o-mini",
        )

    monkeypatch.setattr(adapter_module, "acompletion", failing_acompletion)

    result = await attempt(registry, record.id, "gpt-4o-mini", [{"role": "user", "content": "hi"}])

    assert result.ok is False
    assert result.error.status_code == 400
    assert result.error.code == "bad_request"
    assert canary not in str(result.error)
    assert canary not in result.error.message


@pytest.mark.asyncio
async def test_service_unavailable_error_mapped(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="svc-key")
    )

    async def failing_acompletion(**kwargs):
        raise ServiceUnavailableError(
            message="service unavailable",
            llm_provider="openai",
            model="gpt-4o-mini",
        )

    monkeypatch.setattr(adapter_module, "acompletion", failing_acompletion)

    result = await attempt(registry, record.id, "gpt-4o-mini", [{"role": "user", "content": "hi"}])

    assert result.ok is False
    assert result.error.status_code == 503
    assert result.error.code == "service_unavailable"


@pytest.mark.asyncio
async def test_internal_server_error_mapped(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="ise-key")
    )

    async def failing_acompletion(**kwargs):
        raise InternalServerError(
            message="internal server error",
            llm_provider="openai",
            model="gpt-4o-mini",
        )

    monkeypatch.setattr(adapter_module, "acompletion", failing_acompletion)

    result = await attempt(registry, record.id, "gpt-4o-mini", [{"role": "user", "content": "hi"}])

    assert result.ok is False
    assert result.error.status_code == 500
    assert result.error.code == "internal_error"


@pytest.mark.asyncio
async def test_timeout_error_scrubbed_and_mapped(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    canary = "sk-test-timeout-key"
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key=canary)
    )

    async def failing_acompletion(**kwargs):
        raise Timeout(
            message=f"timeout, key={canary}",
            llm_provider="openai",
            model="gpt-4o-mini",
        )

    monkeypatch.setattr(adapter_module, "acompletion", failing_acompletion)

    result = await attempt(registry, record.id, "gpt-4o-mini", [{"role": "user", "content": "hi"}])

    assert result.ok is False
    assert result.error.status_code == 408
    assert result.error.code == "timeout"
    assert canary not in str(result.error)
    assert canary not in result.error.message


@pytest.mark.asyncio
async def test_generic_exception_scrubbed(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    canary = "canary-in-generic-exception"
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key=canary)
    )

    async def failing_acompletion(**kwargs):
        raise RuntimeError(f"unexpected error with key={canary}")

    monkeypatch.setattr(adapter_module, "acompletion", failing_acompletion)

    result = await attempt(registry, record.id, "gpt-4o-mini", [{"role": "user", "content": "hi"}])

    assert result.ok is False
    assert result.error.status_code is None
    assert result.error.code == "unexpected_error"
    assert canary not in str(result.error)
    assert canary not in result.error.message


# -- deleted key -> CandidateGoneError, not a generic 500 --------------------


@pytest.mark.asyncio
async def test_attempt_on_deleted_key_raises_candidate_gone(registry: PoolRegistry):
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="del-key")
    )
    await registry.remove(record.id)

    with pytest.raises(CandidateGoneError):
        await attempt(registry, record.id, "gpt-4o-mini", [{"role": "user", "content": "hi"}])


def test_candidate_gone_error_is_key_not_found_error():
    assert issubclass(CandidateGoneError, KeyNotFoundError)


# -- RETRY_BUDGET knob, default + overridable ---------------------------------


def test_retry_budget_default_and_overridable(monkeypatch: pytest.MonkeyPatch):
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        assert get_settings().retry_budget == 3

        monkeypatch.setenv("PALIMPSEST_RETRY_BUDGET", "5")
        get_settings.cache_clear()
        assert get_settings().retry_budget == 5
    finally:
        get_settings.cache_clear()


def test_no_retry_loop_in_adapter_module():
    import ast

    tree = ast.parse(pathlib.Path("app/provider/adapter.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.While, ast.AsyncFor, ast.For)):
            raise AssertionError(f"retry loop found in adapter.py: {ast.dump(node)}")
    assert not any(
        isinstance(node, ast.AsyncFor) for node in ast.walk(tree)
    )
