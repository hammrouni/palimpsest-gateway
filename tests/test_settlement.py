"""Settlement: exactly-once metering, request events and quota reads."""
from __future__ import annotations

import asyncio
import inspect
import json
import pathlib
import secrets
import sys
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis.aioredis import FakeRedis

from app.pool.events import _EVENTS_KEY
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.router.failover import AttemptRecord, FailoverTrace
from app.settlement.read import get_event, remaining, utilization, utilization_view
from app.settlement.settle import (
    SettlementInput,
    SettlementResult,
    _event_key,
    _settled_marker_key,
    _used_key,
    settle,
)

# app/settlement/__init__.py re-exports the `settle` function, shadowing the
# `settle` submodule's own name on the `app.settlement` package object — fetch
# the actual module via sys.modules so monkeypatching `_now` targets it, not
# the function.
settle_module = sys.modules["app.settlement.settle"]


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def registry(redis):
    return PoolRegistry(redis)


async def _make_key(
    registry: PoolRegistry,
    *,
    daily_cap: float | None = None,
    reset_at: datetime | None = None,
    reset_period_hours: int = 24,
):
    return await registry.create(
        KeyCreate(
            provider=Provider.OPENAI,
            models=["gpt-4o-mini"],
            key=f"sk-test-{secrets.token_hex(16)}",
            daily_cap=daily_cap,
            reset_at=reset_at,
            reset_period_hours=reset_period_hours,
        )
    )


def _attempt(key_id: str, outcome: str, status_code: int | None = None) -> AttemptRecord:
    return AttemptRecord(
        key_id=key_id,
        provider=Provider.OPENAI,
        classification=None if outcome == "success" else "transient",
        outcome=outcome,
        status_code=status_code,
    )


def _trace(attempts: tuple[AttemptRecord, ...], terminal: str, retry_count: int = 0) -> FailoverTrace:
    return FailoverTrace(model="gpt-4o-mini", attempts=attempts, terminal=terminal, retry_count=retry_count)


def _input(request_id: str, trace: FailoverTrace, *, status_code: int, reported_cost=None) -> SettlementInput:
    return SettlementInput(
        request_id=request_id,
        account_id="acct-1",
        session_id="sess-1",
        model="gpt-4o-mini",
        trace=trace,
        status_code=status_code,
        latency_ms=42,
        stream=False,
        reported_cost=reported_cost,
    )


# -- successful single-attempt settlement ------------------------------------------


@pytest.mark.asyncio
async def test_success_increments_serving_key_and_writes_event(redis, registry):
    key = await _make_key(registry, daily_cap=10)
    trace = _trace((_attempt(key.id, "success"),), "success")
    inp = _input("req-1", trace, status_code=200)

    result = await settle(inp, redis=redis)

    assert result == SettlementResult(settled_key=key.id, settled=True, event_key=_event_key("req-1"), new_used=1)
    assert await redis.get(_used_key(key.id)) == b"1"
    event = await get_event("req-1", redis=redis)
    assert event["settled"] == 1
    assert event["settled_key"] == key.id
    assert event["chain"] == [key.id]
    assert event["outcome"] == "success"
    assert event["status_code"] == 200
    assert event["request_id"] == "req-1"
    assert event["account_id"] == "acct-1"
    assert event["session_id"] == "sess-1"
    assert event["model"] == "gpt-4o-mini"
    assert event["attempts"] == 1
    assert event["latency_ms"] == 42
    assert event["stream"] is False


# -- terminal failure -> no increment, event still written -------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal,status_code",
    [("fatal", 400), ("budget_exhausted", 503), ("pool_exhausted", 503)],
)
async def test_terminal_failure_no_increment_but_event_written(redis, registry, terminal, status_code):
    key_a = await _make_key(registry, daily_cap=10)
    key_b = await _make_key(registry, daily_cap=10)
    trace = _trace((_attempt(key_a.id, "failed", 500), _attempt(key_b.id, "failed", 500)), terminal)
    inp = _input(f"req-{terminal}", trace, status_code=status_code)

    result = await settle(inp, redis=redis)

    assert result.settled is False
    assert result.new_used is None
    assert await redis.get(_used_key(key_a.id)) is None
    assert await redis.get(_used_key(key_b.id)) is None
    event = await get_event(f"req-{terminal}", redis=redis)
    assert event["settled"] == 0
    assert event["settled_key"] is None
    assert event["outcome"] == terminal


# -- failover chain, only the winner is charged -------------------------------------


@pytest.mark.asyncio
async def test_failover_chain_only_winner_charged(redis, registry):
    key_a = await _make_key(registry, daily_cap=10)
    key_b = await _make_key(registry, daily_cap=10)
    key_c = await _make_key(registry, daily_cap=10)
    trace = _trace(
        (_attempt(key_a.id, "failed", 500), _attempt(key_b.id, "failed", 500), _attempt(key_c.id, "success")),
        "success",
        retry_count=2,
    )
    inp = _input("req-chain", trace, status_code=200)

    result = await settle(inp, redis=redis)

    assert result.settled_key == key_c.id
    assert await redis.get(_used_key(key_a.id)) is None
    assert await redis.get(_used_key(key_b.id)) is None
    assert await redis.get(_used_key(key_c.id)) == b"1"
    event = await get_event("req-chain", redis=redis)
    assert event["chain"] == [key_a.id, key_b.id, key_c.id]
    assert event["settled_key"] == key_c.id


# -- reported_cost correction seam --------------------------------------------------


@pytest.mark.asyncio
async def test_reported_cost_none_is_delta_one(redis, registry):
    key = await _make_key(registry, daily_cap=10)
    trace = _trace((_attempt(key.id, "success"),), "success")
    await settle(_input("req-none", trace, status_code=200, reported_cost=None), redis=redis)
    assert await redis.get(_used_key(key.id)) == b"1"


@pytest.mark.asyncio
async def test_reported_cost_zero_is_delta_zero_settled_false(redis, registry):
    key = await _make_key(registry, daily_cap=10)
    trace = _trace((_attempt(key.id, "success"),), "success")
    result = await settle(_input("req-zero", trace, status_code=200, reported_cost=0), redis=redis)
    assert result.settled is False
    assert await redis.get(_used_key(key.id)) is None
    event = await get_event("req-zero", redis=redis)
    assert event is not None and event["settled"] == 0


@pytest.mark.asyncio
async def test_reported_cost_three_is_delta_two(redis, registry):
    key = await _make_key(registry, daily_cap=10)
    trace = _trace((_attempt(key.id, "success"),), "success")
    result = await settle(_input("req-three", trace, status_code=200, reported_cost=3), redis=redis)
    assert result.new_used == 2
    assert await redis.get(_used_key(key.id)) == b"2"


@pytest.mark.asyncio
async def test_negative_reported_cost_clamped_no_negative_counter(redis, registry):
    key = await _make_key(registry, daily_cap=10)
    trace = _trace((_attempt(key.id, "success"),), "success")
    result = await settle(_input("req-neg", trace, status_code=200, reported_cost=-5), redis=redis)
    assert result.settled is False
    assert await redis.get(_used_key(key.id)) is None


# -- over-cap guard, zero incidents (SM-C1) ------------------------------------------


@pytest.mark.asyncio
async def test_guard_blocks_increment_at_cap(redis, registry):
    key = await _make_key(registry, daily_cap=2)
    trace = lambda rid: _trace((_attempt(key.id, "success"),), "success")  # noqa: E731

    await settle(_input("req-1", trace("req-1"), status_code=200), redis=redis)
    await settle(_input("req-2", trace("req-2"), status_code=200), redis=redis)
    assert await redis.get(_used_key(key.id)) == b"2"

    result = await settle(_input("req-3", trace("req-3"), status_code=200), redis=redis)

    assert result.settled is False
    assert await redis.get(_used_key(key.id)) == b"2"
    event = await get_event("req-3", redis=redis)
    assert event["settled"] == 0


@pytest.mark.asyncio
async def test_hundred_concurrent_settlements_never_exceed_cap(redis, registry):
    key = await _make_key(registry, daily_cap=50)

    async def _settle_one(i: int):
        trace = _trace((_attempt(key.id, "success"),), "success")
        return await settle(_input(f"req-concurrent-{i}", trace, status_code=200), redis=redis)

    results = await asyncio.gather(*(_settle_one(i) for i in range(100)))

    used = int(await redis.get(_used_key(key.id)))
    assert used == 50
    assert sum(1 for r in results if r.settled) == 50


# -- exactly-once (duplicate request_id) ---------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_request_id_is_a_noop(redis, registry):
    key = await _make_key(registry, daily_cap=10)
    trace = _trace((_attempt(key.id, "success"),), "success")
    inp = _input("req-dup", trace, status_code=200)

    first = await settle(inp, redis=redis)
    second = await settle(inp, redis=redis)

    assert first.settled is True
    assert second.settled is False
    assert await redis.get(_used_key(key.id)) == b"1"
    event = await get_event("req-dup", redis=redis)
    assert event["settled_key"] == key.id  # unchanged from the first (real) settlement


# -- window reset on boundary crossing -----------------------------------------------


@pytest.mark.asyncio
async def test_window_reset_on_boundary_crossing(redis, registry, monkeypatch):
    anchor = datetime.now(UTC) + timedelta(seconds=5)
    key = await _make_key(registry, daily_cap=100, reset_at=anchor, reset_period_hours=1)

    before_boundary = anchor - timedelta(minutes=1)
    monkeypatch.setattr(settle_module, "_now", lambda: before_boundary)
    trace1 = _trace((_attempt(key.id, "success"),), "success")
    result1 = await settle(_input("req-w1", trace1, status_code=200), redis=redis)
    assert result1.new_used == 1

    after_boundary = anchor + timedelta(hours=1, seconds=1)
    monkeypatch.setattr(settle_module, "_now", lambda: after_boundary)
    trace2 = _trace((_attempt(key.id, "success"),), "success")
    result2 = await settle(_input("req-w2", trace2, status_code=200), redis=redis)

    assert result2.new_used == 1  # reset to 0, then +1 -- not 2
    assert await redis.get(_used_key(key.id)) == b"1"


# -- remaining()/utilization() -------------------------------------------------------


@pytest.mark.asyncio
async def test_remaining_capped_and_uncapped(redis, registry):
    capped = await _make_key(registry, daily_cap=10)
    uncapped = await _make_key(registry, daily_cap=None)

    assert await remaining(capped.id, redis=redis) == 10  # zero traffic -> full cap
    assert await remaining(uncapped.id, redis=redis) is None

    trace = _trace((_attempt(capped.id, "success"),), "success")
    await settle(_input("req-r1", trace, status_code=200), redis=redis)
    assert await remaining(capped.id, redis=redis) == 9


@pytest.mark.asyncio
async def test_utilization_aggregate_matches_per_key_sum(redis, registry):
    key_a = await _make_key(registry, daily_cap=10)
    key_b = await _make_key(registry, daily_cap=20)

    await settle(_input("req-u1", _trace((_attempt(key_a.id, "success"),), "success"), status_code=200), redis=redis)
    await settle(_input("req-u2", _trace((_attempt(key_b.id, "success"),), "success"), status_code=200), redis=redis)

    result = await utilization(redis=redis)

    by_id = {k.key_id: k for k in result.per_key}
    assert by_id[key_a.id].used == 1
    assert by_id[key_b.id].used == 1
    assert result.total_used == sum(k.used for k in result.per_key)
    assert result.total_cap == 30.0
    assert result.fraction_served == pytest.approx(2 / 30)


# -- get_event for unknown request ---------------------------------------------------


@pytest.mark.asyncio
async def test_get_event_unknown_request_returns_none(redis):
    assert await get_event("nonexistent-request-id", redis=redis) is None


# -- settlement never alters the client response ------------------------------------


class _FaultyScript:
    async def __call__(self, *args, **kwargs):
        from redis.exceptions import ConnectionError as RedisConnectionError

        raise RedisConnectionError("simulated Redis outage")


class _FaultyRedis:
    """Delegates everything to a real client except script execution (fault injection)."""

    def __init__(self, real):
        self._real = real

    def register_script(self, _src):
        return _FaultyScript()

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.mark.asyncio
async def test_redis_failure_never_raises_and_response_is_unaffected(redis, registry, caplog):
    key = await _make_key(registry, daily_cap=10)
    trace = _trace((_attempt(key.id, "success"),), "success")
    inp = _input("req-fault", trace, status_code=200)

    with caplog.at_level("ERROR", logger="palimpsest.settlement"):
        result = await settle(inp, redis=_FaultyRedis(redis))

    assert result == SettlementResult(
        settled_key=None, settled=False, event_key=_event_key("req-fault"), new_used=None
    )
    assert any("settlement_failed" in message for message in caplog.messages)
    # The client-facing "response" this simulates is just inp/status_code -- settle()
    # raised nothing, so a caller's already-built response is untouched by construction.


# -- canary -- no key material or provider identity in the event -------------------


@pytest.mark.asyncio
async def test_no_key_material_or_provider_identity_leaks(redis, registry, caplog):
    key = await _make_key(registry, daily_cap=10)
    secret_fragment = "sk-super-secret-provider-key-material"
    trace = _trace((_attempt(key.id, "success"),), "success")
    # Traces never carry key material; this canary proves settle() adds no leak
    # even when unrelated context contains a secret-looking string.
    inp = SettlementInput(
        request_id="req-canary",
        account_id="acct-1",
        session_id=secret_fragment,  # a plausible leak vector: any free-text field
        model="gpt-4o-mini",
        trace=trace,
        status_code=200,
        latency_ms=1,
        stream=False,
    )

    with caplog.at_level("INFO"):
        await settle(inp, redis=redis)

    event = await get_event("req-canary", redis=redis)
    serialized = str(event)
    assert "openai" not in serialized.lower()
    assert Provider.OPENAI.value not in serialized
    for record in caplog.records:
        assert secret_fragment not in record.getMessage() or "session_id" in record.getMessage()


# -- atomicity -- no partial state on failure, one round-trip on success -----------


@pytest.mark.asyncio
async def test_fault_injection_leaves_no_partial_state(redis, registry):
    key = await _make_key(registry, daily_cap=10)
    trace = _trace((_attempt(key.id, "success"),), "success")
    inp = _input("req-atomic", trace, status_code=200)

    await settle(inp, redis=_FaultyRedis(redis))

    assert await redis.get(_used_key(key.id)) is None
    assert await get_event("req-atomic", redis=redis) is None
    assert await redis.get(_settled_marker_key("req-atomic")) is None


def test_settle_issues_a_single_atomic_script_call():
    """Static check: the counter increment and the event write happen inside the
    same ``await script(...)`` call -- no separate awaited write commands exist
    that could land independently of each other."""
    source = inspect.getsource(settle)
    assert source.count("await script(") == 1
    for forbidden in (".hset(", ".incr(", ".incrby(", "await redis.set("):
        assert forbidden not in source


# -- static scan -- used-counter writes live only in app/settlement ----------------


_WRITE_VERBS = ("set(", "incr(", "incrby(", "delete(", "hset(")


def test_used_counter_written_only_in_settlement():
    app_dir = pathlib.Path("app")
    violations = []
    for path in app_dir.rglob("*.py"):
        if "settlement" in path.parts or "tests" in path.parts:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            if "used" in line and any(verb in line for verb in _WRITE_VERBS):
                violations.append(f"{path}:{lineno}: {line.strip()}")
    assert violations == []


# -- fraction_served excludes uncapped key usage from numerator -----------------


@pytest.mark.asyncio
async def test_fraction_served_does_not_include_uncapped_usage(redis, registry):
    capped = await _make_key(registry, daily_cap=10)
    uncapped = await _make_key(registry, daily_cap=None)

    trace_capped = _trace((_attempt(capped.id, "success"),), "success")
    await settle(_input("req-fs-1", trace_capped, status_code=200), redis=redis)

    # Directly set uncapped key's used to a high value (simulating prior traffic)
    await redis.set(_used_key(uncapped.id), 100)

    result = await utilization(redis=redis)

    # total_used includes ALL keys (capped:1 + uncapped:100) per spec
    assert result.total_used == 101
    # total_cap only includes capped keys
    assert result.total_cap == 10.0
    # fraction_served must use capped-only numerator: 1/10 = 0.1, NOT 101/10 = 10.1
    assert result.fraction_served == 0.1


# -- failover event mirror is written for multi-attempt chains ------------------


@pytest.mark.asyncio
async def test_failover_event_mirror_written(redis, registry):
    key_a = await _make_key(registry, daily_cap=10)
    key_b = await _make_key(registry, daily_cap=10)
    key_c = await _make_key(registry, daily_cap=10)
    trace = _trace(
        (
            _attempt(key_a.id, "failed", 500),
            _attempt(key_b.id, "failed", 500),
            _attempt(key_c.id, "success"),
        ),
        "success",
        retry_count=2,
    )
    inp = _input("req-mirror", trace, status_code=200)

    result = await settle(inp, redis=redis)

    assert result.settled is True
    assert result.new_used == 1

    events = await redis.lrange(_EVENTS_KEY, 0, -1)
    assert len(events) == 1
    record = json.loads(events[0])
    assert record["request_id"] == "req-mirror"
    assert record["chain"] == [key_a.id, key_b.id, key_c.id]
    assert record["source_key_id"] == key_a.id
    assert record["target_key_id"] == key_c.id
    assert record["outcome"] == "success"
    assert record["attempts"] == 3


# -- failover append failure (non-RedisError) does not mask settlement ---------


@pytest.mark.asyncio
async def test_failover_append_failure_does_not_mask_settlement(redis, registry, monkeypatch):
    key_a = await _make_key(registry, daily_cap=10)
    key_b = await _make_key(registry, daily_cap=10)
    trace = _trace(
        (_attempt(key_a.id, "failed", 500), _attempt(key_b.id, "success")),
        "success",
        retry_count=1,
    )
    inp = _input("req-fo-isolate", trace, status_code=200)

    # Simulate append_failover_event raising a non-RedisError (e.g. TypeError
    # from json.dumps on unexpected data) — must not propagate or mask success.
    async def _faulty_append(*args, **kwargs):
        raise TypeError("simulated serialization failure")

    monkeypatch.setattr(settle_module, "append_failover_event", _faulty_append)

    result = await settle(inp, redis=redis)

    assert result.settled is True
    assert result.settled_key == key_b.id
    assert result.new_used == 1
    event = await get_event("req-fo-isolate", redis=redis)
    assert event is not None and event["settled"] == 1
    assert event["chain"] == [key_a.id, key_b.id]


# -- corrupted Redis counter values are handled gracefully -----------------


@pytest.mark.asyncio
async def test_used_returns_zero_on_corrupt_counter(redis):
    """_used() degrades to 0 on a non-numeric value instead of crashing.

    A corrupt pgw:pool:key:{k}:used value must not propagate a
    ValueError/TypeError to utilization() or utilization_view().
    """
    from app.settlement.read import _used

    # Seed a pool key with a corrupt used counter (non-numeric string).
    await redis.set(_used_key("corrupt-key"), "not-a-number")

    result = await _used(redis, "corrupt-key")
    assert result == 0  # degraded to 0, not a crash


@pytest.mark.asyncio
async def test_utilization_does_not_crash_on_corrupt_counter(redis, registry, caplog):
    """utilization() survives a corrupted used counter (non-numeric string)
    and returns a view with the corrupt key defaulted to 0, never a 500."""
    import logging as _logging

    key = await _make_key(registry, daily_cap=100)

    # Settle one real request so the key has a valid used counter (1).
    trace = _trace((_attempt(key.id, "success"),), "success")
    await settle(_input("req-ac2-ok", trace, status_code=200), redis=redis)

    # Corrupt the used counter with a non-numeric string.
    await redis.set(_used_key(key.id), "ABC-not-numeric")

    with caplog.at_level(_logging.WARNING, logger="palimpsest.settlement.read"):
        result = await utilization(redis=redis)

    by_id = {k.key_id: k for k in result.per_key}
    assert key.id in by_id
    assert by_id[key.id].used == 0  # degraded, not crashed
    assert any("key_counter_corrupt" in m for m in caplog.messages)


@pytest.mark.asyncio
async def test_remaining_does_not_crash_on_corrupt_counter(redis, registry):
    """remaining() (which calls _used internally) also degrades on
    a corrupted used counter."""
    key = await _make_key(registry, daily_cap=100)
    await redis.set(_used_key(key.id), "corrupt!!")
    # remaining() should return max(0, 100 - 0) = 100, not crash
    result = await remaining(key.id, redis=redis)
    assert result == 100


# -- settlement idempotency marker TTL covers streaming durations -----------------


def test_settle_id_ttl_seconds_default_is_300():
    """_SETTLE_ID_TTL_SECONDS default is 300s (5 min), well beyond the
    60s provider_timeout so a long streaming response never expires the
    idempotency marker before settlement runs."""
    from app.config import Settings

    settings = Settings()
    assert settings.settle_id_ttl_seconds == 300
    assert settings.settle_id_ttl_seconds > settings.provider_timeout


def test_settle_id_ttl_seconds_is_configurable():
    """The TTL is overridable via PALIMPSEST_SETTLE_ID_TTL_SECONDS."""
    import os

    from app.config import get_settings

    old = os.environ.pop("PALIMPSEST_SETTLE_ID_TTL_SECONDS", None)
    get_settings.cache_clear()
    try:
        os.environ["PALIMPSEST_SETTLE_ID_TTL_SECONDS"] = "600"
        settings = get_settings()
        assert settings.settle_id_ttl_seconds == 600
    finally:
        os.environ.pop("PALIMPSEST_SETTLE_ID_TTL_SECONDS", None)
        if old is not None:
            os.environ["PALIMPSEST_SETTLE_ID_TTL_SECONDS"] = old
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_settle_uses_configured_ttl_in_lua_args(redis, registry, monkeypatch):
    """settle() passes the configured settle_id_ttl_seconds to the
    idempotency marker (KEYS[4]), not a hardcoded 60s constant. The marker
    TTL in Redis must reflect the configurable value."""
    from app.config import Settings

    # Use a distinctive TTL value to verify it flows through the Lua script.
    settings = Settings(settle_id_ttl_seconds=500)
    monkeypatch.setattr(settle_module, "get_settings", lambda: settings)

    key = await _make_key(registry, daily_cap=10)
    trace = _trace((_attempt(key.id, "success"),), "success")
    inp = _input("req-ttl-test", trace, status_code=200)

    await settle(inp, redis=redis)

    # The settled marker (pgw:ops:settled:{request_id}) should have a TTL
    # reflecting the configured 500s, not the old 60s.
    marker_ttl = await redis.ttl(_settled_marker_key("req-ttl-test"))
    assert marker_ttl > 60, f"marker TTL {marker_ttl} <= 60 — the old 60s default is still in use"
    assert 400 <= marker_ttl <= 500, f"marker TTL {marker_ttl} not in [400, 500] range"


# -- fraction_served with uncapped keys ----------------------------------------


@pytest.mark.asyncio
async def test_fraction_served_not_inflated_by_uncapped_keys(redis, registry):
    """utilization() fraction_served uses capped-only usage as the
    numerator so uncapped keys with heavy usage do not inflate the fraction.
    The numerator (total_used_capped) excludes uncapped keys entirely."""
    capped = await _make_key(registry, daily_cap=100)
    uncapped = await _make_key(registry, daily_cap=None)

    # Settle a successful request on the capped key (used=1).
    trace_capped = _trace((_attempt(capped.id, "success"),), "success")
    await settle(_input("req-ac5-capped", trace_capped, status_code=200), redis=redis)

    # Settle a successful request on the uncapped key (used=1 on the key).
    trace_uncapped = _trace((_attempt(uncapped.id, "success"),), "success")
    await settle(_input("req-ac5-uncapped", trace_uncapped, status_code=200), redis=redis)

    # Push the uncapped key's used counter high to simulate heavy usage.
    await redis.set(_used_key(uncapped.id), 500)

    # utilization() — fraction_served must use capped-only numerator.
    util = await utilization(redis=redis)
    # total_used includes ALL keys (capped: 1 + uncapped: 500 = 501)
    assert util.total_used == 501
    # total_cap only includes capped keys (100)
    assert util.total_cap == 100.0
    # fraction_served = capped_used / total_cap = 1 / 100 = 0.01, NOT 501/100
    assert util.fraction_served == 0.01

    # utilization_view() — utilization_pct must also use capped-only numerator.
    view = await utilization_view(redis=redis)
    agg = view["aggregate"]
    # total_used includes the uncapped key's 500
    assert agg["total_used"] == 501
    # total_cap is the capped keys' total (100)
    assert agg["total_cap"] == 100.0
    # utilization_pct = capped_used / total_cap * 100 = 1 / 100 * 100 = 1.0
    assert agg["utilization_pct"] == 1.0

    # The uncapped key's daily_cap is None and remaining is None.
    by_id = {k["key_id"]: k for k in view["keys"]}
    assert by_id[uncapped.id]["daily_cap"] is None
    assert by_id[uncapped.id]["remaining"] is None
    assert by_id[capped.id]["daily_cap"] == 100
    assert by_id[capped.id]["remaining"] == 99
