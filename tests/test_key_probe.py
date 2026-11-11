"""Testing a pool key: one tiny request, a plain verdict, and no side effects."""
import asyncio
import os
from types import SimpleNamespace

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient

import app.pool.probe as probe_module
from app.pool.models import KeyCreate, KeyState, Provider
from app.pool.probe import probe_key
from app.pool.registry import KeyNotFoundError, PoolRegistry
from app.provider.adapter import AttemptResult, ProviderAttemptError
from app.store.operator import OperatorStore
from app.usage.store import UsageStore

KEYS = "/v1/operator/pool/keys"


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


@pytest.fixture
async def operator_headers(fake_redis: FakeRedis) -> dict[str, str]:
    result = await OperatorStore(fake_redis).create_key()
    return {"Authorization": f"Bearer {result['key']}"}


async def _add(registry, models=("gpt-4o-mini",), provider=Provider.OPENAI, key="sk-probe-0001"):
    return await registry.create(KeyCreate(provider=provider, models=list(models), key=key))


def _answer(text="ok", tokens=9):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=text))],
        usage=SimpleNamespace(total_tokens=tokens),
    )
    return AttemptResult(ok=True, response=response)


def _failure(status, message="boom", code="some_code"):
    return AttemptResult(
        ok=False, error=ProviderAttemptError(status, code, message, Provider.OPENAI)
    )


def _script(monkeypatch, outcome):
    calls = []

    async def fake(registry, key_id, model, messages, **params):
        calls.append({"key_id": key_id, "model": model, "messages": messages, "params": params})
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(probe_module, "attempt", fake)
    return calls


@pytest.mark.asyncio
async def test_a_working_key_reports_the_reply_and_timing(registry, monkeypatch):
    record = await _add(registry)
    calls = _script(monkeypatch, _answer("ok  \n", tokens=11))

    result = await probe_key(registry, record.id)

    assert result["ok"] is True and result["verdict"] == "ok"
    assert result["model"] == "gpt-4o-mini" and result["provider"] == "openai"
    assert result["reply"] == "ok" and result["tokens"] == 11
    assert isinstance(result["latency_ms"], int)
    # One small request, nothing like a full completion.
    assert len(calls) == 1 and calls[0]["params"]["max_tokens"] <= 32
    assert len(calls[0]["messages"]) == 1


@pytest.mark.asyncio
async def test_the_first_model_is_the_default_and_another_can_be_chosen(registry, monkeypatch):
    record = await _add(registry, models=("a-model", "b-model"))
    calls = _script(monkeypatch, _answer())

    assert (await probe_key(registry, record.id))["model"] == "a-model"
    assert (await probe_key(registry, record.id, "b-model"))["model"] == "b-model"
    assert [c["model"] for c in calls] == ["a-model", "b-model"]

    with pytest.raises(ValueError, match="not one of"):
        await probe_key(registry, record.id, "other-model")


@pytest.mark.asyncio
async def test_unknown_key_is_not_found(registry):
    with pytest.raises(KeyNotFoundError):
        await probe_key(registry, "does-not-exist")


@pytest.mark.parametrize(
    "outcome, verdict, ok_key",
    [
        (_failure(401, "Incorrect API key provided"), "key_rejected", False),
        (_failure(404, "model not found"), "model_not_found", False),
        (_failure(429, "Too many requests, slow down"), "rate_limited", True),
        (_failure(429, "You exceeded your current quota"), "no_credits", True),
        (_failure(402, "payment required"), "no_credits", True),
        (_failure(503, "service overloaded"), "provider_error", True),
        (_failure(None, "Cannot connect to host localhost:11434"), "unreachable", True),
        (_failure(None, "Connection refused"), "unreachable", True),
        (_failure(None, "Request timed out.", code="timeout"), "timeout", True),
        (_failure(400, "unsupported parameter: max_tokens"), "bad_request", True),
    ],
)
@pytest.mark.asyncio
async def test_each_failure_gets_a_plain_verdict(registry, monkeypatch, outcome, verdict, ok_key):
    record = await _add(registry)
    _script(monkeypatch, outcome)

    result = await probe_key(registry, record.id)

    assert result["ok"] is False and result["verdict"] == verdict
    assert result["message"]
    assert result["status_code"] == outcome.error.status_code


@pytest.mark.asyncio
async def test_a_failed_test_never_changes_key_state(registry, redis, monkeypatch):
    """Live traffic would suspend a rejected key and pause a limited one; a test must not."""
    record = await _add(registry)

    for outcome in (_failure(401, "bad key"), _failure(429, "slow down"), _failure(404, "no model")):
        _script(monkeypatch, outcome)
        await probe_key(registry, record.id)

    assert await registry.effective_state(record.id) == KeyState.ACTIVE
    assert [k.id for k in await registry.routable_keys("gpt-4o-mini")] == [record.id]
    assert await UsageStore(redis).recent(10) == []
    assert await redis.get(f"pgw:pool:key:{record.id}:used") is None


@pytest.mark.asyncio
async def test_provider_text_is_shown_with_key_shaped_strings_removed(registry, monkeypatch):
    record = await _add(registry)
    _script(monkeypatch, _failure(401, "Incorrect API key provided: sk-live-abcdef1234567890"))

    result = await probe_key(registry, record.id)

    assert "Incorrect API key provided" in result["detail"]
    assert "abcdef1234567890" not in result["detail"]


@pytest.mark.asyncio
async def test_a_hanging_provider_times_out(registry, monkeypatch):
    record = await _add(registry)

    async def hang(*args, **kwargs):
        await asyncio.sleep(5)

    monkeypatch.setattr(probe_module, "attempt", hang)
    monkeypatch.setattr(probe_module, "_PROBE_TIMEOUT_S", 0.05)

    result = await probe_key(registry, record.id)
    assert result["verdict"] == "timeout" and result["ok"] is False


# -- the operator API ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_runs_the_test_and_audits_it(
    async_client: AsyncClient, operator_headers: dict[str, str], monkeypatch
):
    added = await async_client.post(
        KEYS, headers=operator_headers,
        json={"provider": "openai", "models": ["m1", "m2"], "key": "sk-api-0001"},
    )
    key_id = added.json()["id"]
    _script(monkeypatch, _answer("ok"))

    resp = await async_client.post(f"{KEYS}/{key_id}/test", headers=operator_headers)
    assert resp.status_code == 200
    assert resp.json()["ok"] is True and resp.json()["model"] == "m1"

    chosen = await async_client.post(
        f"{KEYS}/{key_id}/test", headers=operator_headers, json={"model": "m2"}
    )
    assert chosen.json()["model"] == "m2"

    audit = await async_client.get("/operator/v1/audit", headers=operator_headers)
    actions = [row["action"] for row in audit.json().get("data", audit.json())]
    assert actions.count("pool_key.test") == 2


@pytest.mark.asyncio
async def test_api_reports_a_failure_as_a_normal_result(
    async_client: AsyncClient, operator_headers: dict[str, str], monkeypatch
):
    key_id = (
        await async_client.post(
            KEYS, headers=operator_headers,
            json={"provider": "openai", "models": ["m"], "key": "sk-api-0002"},
        )
    ).json()["id"]
    _script(monkeypatch, _failure(401, "bad key"))

    resp = await async_client.post(f"{KEYS}/{key_id}/test", headers=operator_headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["ok"] is False and body["verdict"] == "key_rejected"

    listed = await async_client.get(f"{KEYS}/{key_id}", headers=operator_headers)
    assert listed.json()["state"] == "active"


@pytest.mark.asyncio
async def test_api_errors(async_client: AsyncClient, operator_headers: dict[str, str], monkeypatch):
    missing = await async_client.post(f"{KEYS}/nope/test", headers=operator_headers)
    assert missing.status_code == 404

    key_id = (
        await async_client.post(
            KEYS, headers=operator_headers,
            json={"provider": "openai", "models": ["m"], "key": "sk-api-0003"},
        )
    ).json()["id"]
    wrong = await async_client.post(
        f"{KEYS}/{key_id}/test", headers=operator_headers, json={"model": "other"}
    )
    assert wrong.status_code == 422 and wrong.json()["error"]["param"] == "model"

    anonymous = await async_client.post(f"{KEYS}/{key_id}/test")
    assert anonymous.status_code == 401


@pytest.mark.asyncio
async def test_litellm_class_names_are_stripped_from_the_detail(registry, monkeypatch):
    record = await _add(registry)
    _script(
        monkeypatch,
        _failure(401, "litellm.AuthenticationError: AuthenticationError: OpenAIException - Incorrect API key"),
    )
    assert (await probe_key(registry, record.id))["detail"] == "Incorrect API key"


@pytest.mark.asyncio
async def test_a_no_balance_429_is_a_billing_verdict_not_a_rate_limit(registry, monkeypatch):
    """Z.ai answers a paid model with 429 'Insufficient balance or no resource package'."""
    record = await _add(registry, provider=Provider.ZAI, models=("glm-5",))
    _script(monkeypatch, _failure(429, "Insufficient balance or no resource package. Please recharge."))

    result = await probe_key(registry, record.id)

    assert result["verdict"] == "no_credits"
    assert "free models" in result["message"]
    assert result["status_code"] == 429


# -- testing every model of a key ---------------------------------------------------


def _per_model(monkeypatch, outcomes: dict):
    seen = []

    async def fake(registry, key_id, model, messages, **params):
        seen.append(model)
        return outcomes[model]

    monkeypatch.setattr(probe_module, "attempt", fake)
    return seen


@pytest.mark.asyncio
async def test_testing_all_models_says_which_ones_work(registry, monkeypatch):
    record = await _add(registry, models=("free-a", "paid-b", "free-c", "gone-d"))
    seen = _per_model(
        monkeypatch,
        {
            "free-a": _answer(),
            "paid-b": _failure(429, "Insufficient balance or no resource package"),
            "free-c": _answer(),
            "gone-d": _failure(404, "model not found"),
        },
    )

    out = await probe_module.probe_models(registry, record.id)

    assert sorted(seen) == ["free-a", "free-c", "gone-d", "paid-b"]
    assert [r["model"] for r in out["results"]] == ["free-a", "paid-b", "free-c", "gone-d"]
    assert out["working"] == ["free-a", "free-c"]
    assert out["failing"] == ["paid-b", "gone-d"]
    assert [r["verdict"] for r in out["results"]] == ["ok", "no_credits", "ok", "model_not_found"]
    # Like a single test, it changes nothing about the key.
    assert await registry.effective_state(record.id) == KeyState.ACTIVE


@pytest.mark.asyncio
async def test_testing_all_models_runs_a_few_at_a_time(registry, monkeypatch):
    models = tuple(f"m{i}" for i in range(9))
    record = await _add(registry, models=models)
    running = peak = 0

    async def slow(registry, key_id, model, messages, **params):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.01)
        running -= 1
        return _answer()

    monkeypatch.setattr(probe_module, "attempt", slow)
    out = await probe_module.probe_models(registry, record.id)

    assert len(out["working"]) == 9
    assert 1 < peak <= probe_module._PROBE_CONCURRENCY


@pytest.mark.asyncio
async def test_testing_all_models_refuses_a_huge_list(registry, monkeypatch):
    record = await _add(
        registry, models=tuple(f"m{i}" for i in range(probe_module.MAX_MODELS_PER_RUN + 1))
    )
    seen = _per_model(monkeypatch, {})
    with pytest.raises(ValueError, match="at most"):
        await probe_module.probe_models(registry, record.id)
    assert seen == []  # nothing was sent

    with pytest.raises(KeyNotFoundError):
        await probe_module.probe_models(registry, "does-not-exist")


@pytest.mark.asyncio
async def test_api_tests_all_models_and_audits_it(
    async_client: AsyncClient, operator_headers: dict[str, str], monkeypatch
):
    key_id = (
        await async_client.post(
            KEYS, headers=operator_headers,
            json={"provider": "openai", "models": ["good", "bad"], "key": "sk-api-0009"},
        )
    ).json()["id"]
    _per_model(monkeypatch, {"good": _answer(), "bad": _failure(401, "nope")})

    resp = await async_client.post(f"{KEYS}/{key_id}/test-models", headers=operator_headers)
    assert resp.status_code == 200
    assert resp.json()["working"] == ["good"] and resp.json()["failing"] == ["bad"]

    audit = await async_client.get("/operator/v1/audit", headers=operator_headers)
    rows = audit.json().get("data", audit.json())
    assert any(r["action"] == "pool_key.test_models" for r in rows)

    assert (await async_client.post(f"{KEYS}/nope/test-models", headers=operator_headers)).status_code == 404
