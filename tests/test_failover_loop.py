"""The bounded failover loop."""
import os
import pathlib
from datetime import UTC, datetime

import pytest
from fakeredis.aioredis import FakeRedis

import app.router.failover as failover_module
from app.pool.models import KeyCreate, KeyState, Provider
from app.pool.registry import PoolRegistry
from app.provider.adapter import AttemptResult, CandidateGoneError, ProviderAttemptError
from app.router.exhaustion import PoolExhaustedError
from app.router.failover import execute_with_failover


@pytest.fixture(autouse=True)
def _master_key():
    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        os.environ["PALIMPSEST_GATE_MASTER_KEY"] = Fernet.generate_key().decode()


@pytest.fixture
async def registry():
    return PoolRegistry(FakeRedis())


class _FakeResponse:
    id = "chatcmpl-fake"


async def _add_key(
    registry: PoolRegistry,
    *,
    key: str,
    provider: Provider = Provider.OPENAI,
    models: list[str] | None = None,
    daily_cap: float | None = None,
    reset_at: datetime | None = None,
) -> str:
    record = await registry.create(
        KeyCreate(
            provider=provider,
            models=models or ["gpt-4o-mini"],
            key=key,
            daily_cap=daily_cap,
            reset_at=reset_at,
        )
    )
    return record.id


def _error(status_code: int | None, message: str = "err", provider: Provider = Provider.OPENAI):
    return ProviderAttemptError(status_code, "some_code", message, provider)


def _patch_attempts(monkeypatch: pytest.MonkeyPatch, script: dict):
    """Script: key_id -> AttemptResult | Exception | list of either (consumed in order)."""
    calls: list[str] = []

    async def fake(registry, key_id, model, messages, **params):
        calls.append(key_id)
        outcome = script[key_id]
        if isinstance(outcome, list):
            outcome = outcome.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    return calls


MESSAGES = [{"role": "user", "content": "hi"}]


# -- rotate on transient, mark cooldown, trace shape ---------------------------


@pytest.mark.asyncio
async def test_rotates_on_transient_and_marks_cooldown(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    id_a = await _add_key(registry, key="a", daily_cap=100)
    id_b = await _add_key(registry, key="b", daily_cap=50)

    calls = _patch_attempts(
        monkeypatch,
        {
            id_a: AttemptResult(ok=False, error=_error(429, "rate limit reached")),
            id_b: AttemptResult(ok=True, response=_FakeResponse()),
        },
    )

    result, trace = await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert result.ok is True
    assert calls == [id_a, id_b]
    assert trace.retry_count == 1
    assert trace.terminal == "success"
    assert [a.key_id for a in trace.attempts] == [id_a, id_b]
    assert trace.attempts[0].outcome == "failed"
    assert trace.attempts[0].classification == "transient"
    assert trace.attempts[1].outcome == "success"

    # A transient failure pauses the key for that model only: the key stays active
    # for everything else it serves.
    ttl = await registry.redis.ttl(f"pgw:pool:state:{id_a}:model:gpt-4o-mini")
    assert 0 < ttl <= 60
    assert await registry.effective_state(id_a) == KeyState.ACTIVE
    assert await registry.effective_state(id_b) == KeyState.ACTIVE


# -- same-provider rotation precedes cross-provider ----------------------------


@pytest.mark.asyncio
async def test_same_provider_rotation_precedes_cross_provider(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    id_a = await _add_key(registry, key="a", provider=Provider.OPENAI, daily_cap=1000)
    id_b = await _add_key(registry, key="b", provider=Provider.OPENAI, daily_cap=5)
    id_c = await _add_key(registry, key="c", provider=Provider.ANTHROPIC, daily_cap=500)

    calls = _patch_attempts(
        monkeypatch,
        {
            id_a: AttemptResult(ok=False, error=_error(402, "payment required")),
            id_b: AttemptResult(ok=True, response=_FakeResponse()),
            id_c: AttemptResult(ok=True, response=_FakeResponse()),
        },
    )

    result, trace = await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert result.ok is True
    assert calls == [id_a, id_b]
    assert id_c not in calls


# -- exact call counts under RETRY_BUDGET --------------------------------------


@pytest.mark.asyncio
async def test_budget_3_gives_exactly_4_calls_then_budget_exhausted(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    ids = [await _add_key(registry, key=f"k{i}", daily_cap=100 - i) for i in range(4)]
    script = {kid: AttemptResult(ok=False, error=_error(429, "rate limit")) for kid in ids}
    calls = _patch_attempts(monkeypatch, script)

    with pytest.raises(PoolExhaustedError) as exc_info:
        await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert len(calls) == 4
    assert exc_info.value.code == "budget_exhausted"
    assert exc_info.value.retry_count == 3


@pytest.mark.asyncio
async def test_budget_0_gives_exactly_1_call(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    from app.config import get_settings

    monkeypatch.setenv("PALIMPSEST_RETRY_BUDGET", "0")
    get_settings.cache_clear()
    try:
        id_a = await _add_key(registry, key="a", daily_cap=100)
        calls = _patch_attempts(
            monkeypatch, {id_a: AttemptResult(ok=False, error=_error(429, "rate limit"))}
        )

        with pytest.raises(PoolExhaustedError) as exc_info:
            await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

        assert len(calls) == 1
        assert exc_info.value.code == "budget_exhausted"
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_budget_1_with_3_keys_gives_exactly_2_calls(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    from app.config import get_settings

    monkeypatch.setenv("PALIMPSEST_RETRY_BUDGET", "1")
    get_settings.cache_clear()
    try:
        ids = [await _add_key(registry, key=f"b{i}", daily_cap=100 - i) for i in range(3)]
        script = {kid: AttemptResult(ok=False, error=_error(429, "rate limit")) for kid in ids}
        calls = _patch_attempts(monkeypatch, script)

        with pytest.raises(PoolExhaustedError):
            await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

        assert len(calls) == 2
    finally:
        get_settings.cache_clear()


# -- exclusion beats marker deletion — never retry the same key ---------------


@pytest.mark.asyncio
async def test_excluded_key_never_retried_even_if_marker_deleted(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    id_a = await _add_key(registry, key="a", daily_cap=100)
    id_b = await _add_key(registry, key="b", daily_cap=90)
    id_c = await _add_key(registry, key="c", daily_cap=1)

    async def fake(reg, key_id, model, messages, **params):
        if key_id == id_a:
            # Simulate the marker being wiped out from under the loop mid-request.
            await reg.redis.delete(f"pgw:pool:state:{id_a}")
            return AttemptResult(ok=False, error=_error(429, "rate limit"))
        if key_id == id_b:
            return AttemptResult(ok=False, error=_error(429, "rate limit"))
        return AttemptResult(ok=True, response=_FakeResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)

    result, trace = await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert result.ok is True
    key_ids_attempted = [a.key_id for a in trace.attempts]
    assert key_ids_attempted == [id_a, id_b, id_c]
    assert key_ids_attempted.count(id_a) == 1


# -- single key terminates, never an infinite sequence -------------------------


@pytest.mark.asyncio
async def test_single_key_terminates_not_infinite(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    """With exactly one key, per-request exclusion empties the pool after its
    one failure — before the retry budget is ever the limiting factor. The loop must
    still terminate deterministically (pool_exhausted), never loop forever waiting
    for budget."""
    id_a = await _add_key(registry, key="a", daily_cap=100)
    calls = _patch_attempts(
        monkeypatch, {id_a: AttemptResult(ok=False, error=_error(429, "rate limit"))}
    )

    with pytest.raises(PoolExhaustedError) as exc_info:
        await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert len(calls) == 1
    assert exc_info.value.code == "pool_exhausted"


# -- empty candidate universe -> pool_exhausted with reset hint ----------------


@pytest.mark.asyncio
async def test_no_candidates_raises_pool_exhausted_with_next_reset(registry: PoolRegistry):
    id_a = await _add_key(registry, key="a", daily_cap=10)
    await registry.suspend(id_a)

    with pytest.raises(PoolExhaustedError) as exc_info:
        await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert exc_info.value.code == "pool_exhausted"
    assert exc_info.value.retry_count == 0
    assert exc_info.value.next_reset is not None
    assert exc_info.value.trace.terminal == "pool_exhausted"


@pytest.mark.asyncio
async def test_pool_exhausted_body_names_model_only():
    from app.router.exhaustion import render_exhausted

    error = PoolExhaustedError("pool_exhausted", None, retry_count=0)
    rendered = render_exhausted(error, model="gpt-4o-mini")
    assert "gpt-4o-mini" in rendered.message
    for term in ("openai", "anthropic", "google", "openrouter"):
        assert term not in rendered.message.lower()


# -- scrubbing on the failover path itself (adapter already scrubs; verify
#         nothing here re-exposes raw material) -----------------------------------


@pytest.mark.asyncio
async def test_no_key_material_in_trace_or_exception(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    canary = "sk-abc123secretdef456"
    id_a = await _add_key(registry, key=canary, daily_cap=10)

    calls = _patch_attempts(
        monkeypatch,
        {id_a: AttemptResult(ok=False, error=_error(400, "invalid content length"))},
    )

    result, trace = await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert result.ok is False
    assert canary not in str(trace)
    assert canary not in str(result.error)
    assert len(calls) == 1


# -- FailoverTrace chronological order + retry_count -------------------------


@pytest.mark.asyncio
async def test_trace_order_and_retry_count(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    id_a = await _add_key(registry, key="a", daily_cap=100)
    id_b = await _add_key(registry, key="b", daily_cap=90)
    id_c = await _add_key(registry, key="c", daily_cap=80)

    _patch_attempts(
        monkeypatch,
        {
            id_a: AttemptResult(ok=False, error=_error(429, "rate limit")),
            id_b: AttemptResult(ok=False, error=_error(500, "server error")),
            id_c: AttemptResult(ok=True, response=_FakeResponse()),
        },
    )

    result, trace = await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert [a.key_id for a in trace.attempts] == [id_a, id_b, id_c]
    assert trace.retry_count == 2
    assert trace.terminal == "success"


# -- static scan — mark_* only called from app/router/ ------------------------


def test_mark_calls_originate_only_from_app_router():
    app_dir = pathlib.Path("app")
    offenders = []
    for path in app_dir.rglob("*.py"):
        if path.as_posix().startswith("app/router/") or path.as_posix().startswith("app/pool/"):
            continue
        text = path.read_text(encoding="utf-8")
        if "mark_exhausted(" in text or "mark_cooldown(" in text:
            offenders.append(path)
    assert offenders == []


# -- first-byte boundary — no rotation once committed -------------------------


@pytest.mark.asyncio
async def test_first_byte_committed_no_rotation(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    id_a = await _add_key(registry, key="a", daily_cap=100)
    id_b = await _add_key(registry, key="b", daily_cap=90)

    calls = _patch_attempts(
        monkeypatch,
        {
            id_a: AttemptResult(ok=False, error=_error(429, "rate limit")),
            id_b: AttemptResult(ok=True, response=_FakeResponse()),
        },
    )

    result, trace = await execute_with_failover(
        registry, "gpt-4o-mini", MESSAGES, first_byte_committed=True
    )

    assert len(calls) == 1
    assert result.ok is False
    assert trace.retry_count == 0


@pytest.mark.asyncio
async def test_default_rotates(registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch):
    id_a = await _add_key(registry, key="a", daily_cap=100)
    id_b = await _add_key(registry, key="b", daily_cap=90)

    calls = _patch_attempts(
        monkeypatch,
        {
            id_a: AttemptResult(ok=False, error=_error(429, "rate limit")),
            id_b: AttemptResult(ok=True, response=_FakeResponse()),
        },
    )

    result, _trace = await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert len(calls) == 2
    assert result.ok is True


# -- rebuild hook seam ---------------------------------------------------------


@pytest.mark.asyncio
async def test_identity_hook_default_preserves_messages(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    id_a = await _add_key(registry, key="a", daily_cap=100)
    await _add_key(registry, key="b", daily_cap=90)

    seen_messages = []

    async def fake(reg, key_id, model, messages, **params):
        seen_messages.append(messages)
        if key_id == id_a:
            return AttemptResult(ok=False, error=_error(429, "rate limit"))
        return AttemptResult(ok=True, response=_FakeResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)

    await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert seen_messages[0] == MESSAGES
    assert seen_messages[1] == MESSAGES


@pytest.mark.asyncio
async def test_custom_rebuild_hook_output_used_on_retry(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    id_a = await _add_key(registry, key="a", daily_cap=100)
    await _add_key(registry, key="b", daily_cap=90)

    seen_messages = []

    async def fake(reg, key_id, model, messages, **params):
        seen_messages.append(messages)
        if key_id == id_a:
            return AttemptResult(ok=False, error=_error(429, "rate limit"))
        return AttemptResult(ok=True, response=_FakeResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)

    rebuilt = [{"role": "system", "content": "[Resume]"}, MESSAGES[0]]

    async def custom_hook(messages, session, attempt_index):
        return rebuilt

    await execute_with_failover(registry, "gpt-4o-mini", MESSAGES, rebuild_hook=custom_hook)

    assert seen_messages[0] == MESSAGES
    assert seen_messages[1] == rebuilt


# -- static scan — no retry adapters anywhere ---------------------------------


def test_no_retry_adapters_in_app():
    app_dir = pathlib.Path("app")
    forbidden = ("litellm.Router", "num_retries", "fallbacks", "urllib3.Retry", "tenacity")
    for path in app_dir.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path} contains forbidden token {token!r}"


def test_only_failover_module_has_a_retry_loop():
    router_dir = pathlib.Path("app/router")
    for path in router_dir.glob("*.py"):
        if path.name == "failover.py":
            continue
        text = path.read_text(encoding="utf-8")
        assert "while True" not in text


# -- no counter writes, no cross-request state --------------------------------


@pytest.mark.asyncio
async def test_writes_only_state_markers_never_used_counters(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    id_a = await _add_key(registry, key="a", daily_cap=100)
    id_b = await _add_key(registry, key="b", daily_cap=90)

    before = set(await registry.redis.keys("pgw:pool:*"))

    _patch_attempts(
        monkeypatch,
        {
            id_a: AttemptResult(ok=False, error=_error(429, "rate limit")),
            id_b: AttemptResult(ok=True, response=_FakeResponse()),
        },
    )

    await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    after = set(await registry.redis.keys("pgw:pool:*"))
    new_keys = after - before
    for key in new_keys:
        decoded = key.decode() if isinstance(key, bytes) else key
        assert ":used" not in decoded


def test_no_module_level_mutable_state():
    text = pathlib.Path("app/router/failover.py").read_text()
    # No module-level dict/list/set assignment that would leak across requests.
    import ast

    tree = ast.parse(text)
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                assert not isinstance(target, ast.Name) or not isinstance(
                    node.value, (ast.Dict, ast.List, ast.Set)
                )


# -- Candidate-gone race: skipped without consuming budget -------------------------


@pytest.mark.asyncio
async def test_candidate_gone_is_skipped_without_consuming_budget(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    id_a = await _add_key(registry, key="a", daily_cap=100)
    await _add_key(registry, key="b", daily_cap=90)

    async def fake(reg, key_id, model, messages, **params):
        if key_id == id_a:
            raise CandidateGoneError(id_a)
        return AttemptResult(ok=True, response=_FakeResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)

    result, trace = await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert result.ok is True
    assert trace.attempts[0].outcome == "skipped"
    assert trace.retry_count == 0


# -- (extended): mark_exhausted TTL verified -------------------------------------


@pytest.mark.asyncio
async def test_exhausted_marker_written_with_ttl(registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch):
    id_a = await _add_key(registry, key="a", daily_cap=100)
    id_b = await _add_key(registry, key="b", daily_cap=90)

    _patch_attempts(
        monkeypatch,
        {
            id_a: AttemptResult(ok=False, error=_error(402, "payment required")),
            id_b: AttemptResult(ok=True, response=_FakeResponse()),
        },
    )

    result, trace = await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert result.ok is True
    state_key = f"pgw:pool:state:{id_a}"
    ttl = await registry.redis.ttl(state_key)
    assert ttl > 0  # mark_exhausted was called and wrote a TTL-expiring marker


# -- (extended): fatal classification writes no state marker --------------------


@pytest.mark.asyncio
async def test_fatal_classification_writes_no_state_marker(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    id_a = await _add_key(registry, key="a", daily_cap=100)
    id_b = await _add_key(registry, key="b", daily_cap=90)

    _patch_attempts(
        monkeypatch,
        {
            id_a: AttemptResult(
                ok=False, error=_error(400, "invalid content length")
            ),
            id_b: AttemptResult(ok=True, response=_FakeResponse()),
        },
    )

    result, trace = await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert result.ok is False
    assert trace.terminal == "fatal"
    assert await registry.redis.exists(f"pgw:pool:state:{id_a}") == 0


# -- (extended): scrubbing across multiple classification terminal paths -----------


@pytest.mark.asyncio
async def test_scrubbing_across_classification_paths(
    monkeypatch: pytest.MonkeyPatch
):
    cases = [
        (429, "key={k} rate limited"),
        (402, "payment failed: key={k}"),
        (400, "quota exceeded for key={k}"),
        (400, "invalid request"),
        (500, "server error key={k}"),
    ]

    for i, (status, msg_template) in enumerate(cases):
        registry = PoolRegistry(FakeRedis())
        canary = f"sk-canary-{i:03d}"
        id_a = await _add_key(registry, key=canary, daily_cap=100)
        id_b = await _add_key(registry, key="backup", daily_cap=90)
        msg = msg_template.format(k=canary)

        _patch_attempts(
            monkeypatch,
            {
                id_a: AttemptResult(ok=False, error=ProviderAttemptError(status, "code", msg, Provider.OPENAI)),
                id_b: AttemptResult(ok=True, response=_FakeResponse()),
            },
        )

        result, trace = await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

        assert canary not in str(trace), f"canary leaked in trace for status={status}"
        assert canary not in str(result.error), f"canary leaked in error for status={status}"


# -- (extended): retry_count == 0 when pool_exhausted on first failure -------------


@pytest.mark.asyncio
async def test_pool_exhausted_retry_count_reflects_consumed_attempts(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    id_a = await _add_key(registry, key="a", daily_cap=100)

    calls = _patch_attempts(
        monkeypatch,
        {id_a: AttemptResult(ok=False, error=_error(429, "rate limit"))},
    )

    with pytest.raises(PoolExhaustedError) as exc_info:
        await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert len(calls) == 1
    assert exc_info.value.code == "pool_exhausted"
    assert exc_info.value.retry_count == 1


# -- (extended): earliest next_reset picks the earliest of multiple keys ------------


@pytest.mark.asyncio
async def test_multi_key_earliest_reset_hint(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    from datetime import datetime, timedelta

    now = datetime.now(UTC)
    next_midnight = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    later = next_midnight + timedelta(hours=12)

    id_earlier = await _add_key(registry, key="earlier", daily_cap=10, reset_at=next_midnight)
    id_later = await _add_key(registry, key="later", daily_cap=10, reset_at=later)

    for kid in (id_earlier, id_later):
        await registry.suspend(kid)

    with pytest.raises(PoolExhaustedError) as exc_info:
        await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert exc_info.value.next_reset is not None
    assert exc_info.value.next_reset == next_midnight

    with pytest.raises(PoolExhaustedError) as exc_info:
        await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert exc_info.value.next_reset is not None
    assert exc_info.value.next_reset == next_midnight


# -- (extended): transient propagation picks same-provider over higher-cap -----


@pytest.mark.asyncio
async def test_transient_failure_prefers_same_provider_on_retry(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    id_openai_low = await _add_key(registry, key="o-low", provider=Provider.OPENAI, daily_cap=1000)
    id_anthropic_high = await _add_key(
        registry, key="a-high", provider=Provider.ANTHROPIC, daily_cap=100
    )
    id_openai_mid = await _add_key(registry, key="o-mid", provider=Provider.OPENAI, daily_cap=500)

    _patch_attempts(
        monkeypatch,
        {
            id_openai_low: AttemptResult(ok=False, error=_error(429, "rate limit")),
            id_anthropic_high: AttemptResult(ok=True, response=_FakeResponse()),
            id_openai_mid: AttemptResult(ok=True, response=_FakeResponse()),
        },
    )

    result, trace = await execute_with_failover(registry, "gpt-4o-mini", MESSAGES)

    assert result.ok is True
    calls = [a.key_id for a in trace.attempts]
    assert calls == [id_openai_low, id_openai_mid]
    assert id_anthropic_high not in calls
