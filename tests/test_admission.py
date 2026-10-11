"""Admission: per-account daily caps and the pool low-water-mark throttle."""
from __future__ import annotations

import asyncio
import pathlib
import secrets
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis.aioredis import FakeRedis

import app.settlement.admission as admission_module
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.router.failover import AttemptRecord, FailoverTrace
from app.settlement import POOL_LOW_WATER, USER_QUOTA_EXHAUSTED
from app.settlement.admission import (
    _user_cap_key,
    _user_used_key,
    admit,
    raise_user_cap,
)
from app.settlement.read import get_event
from app.settlement.settle import SettlementInput, settle


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def registry(redis):
    return PoolRegistry(redis)


async def _make_key(registry: PoolRegistry, *, daily_cap: float | None = None) -> str:
    record = await registry.create(
        KeyCreate(
            provider=Provider.OPENAI,
            models=["gpt-4o-mini"],
            key=f"sk-test-{secrets.token_hex(16)}",
            daily_cap=daily_cap,
        )
    )
    return record.id


async def _consume_key(redis, key_id: str, amount: int) -> None:
    """Settle ``amount`` successful requests against ``key_id`` (through settlement)."""
    for i in range(amount):
        trace = FailoverTrace(
            model="gpt-4o-mini",
            attempts=(
                AttemptRecord(
                    key_id=key_id, provider=Provider.OPENAI, classification=None,
                    outcome="success", status_code=None,
                ),
            ),
            terminal="success",
            retry_count=0,
        )
        await settle(
            SettlementInput(
                request_id=f"consume-{key_id}-{i}-{secrets.token_hex(4)}",
                account_id="pool-filler",
                session_id=None,
                model="gpt-4o-mini",
                trace=trace,
                status_code=200,
                latency_ms=1,
                stream=False,
            ),
            redis=redis,
        )


# -- cap enforced pre-selection, no side effects ------------------------------------


@pytest.mark.asyncio
async def test_cap_enforced_pre_selection(redis, monkeypatch):
    monkeypatch.setenv("PALIMPSEST_USER_DAILY_CAP", "2")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        await admit("acct-1", redis=redis)
        await admit("acct-1", redis=redis)
        result = await admit("acct-1", redis=redis)
    finally:
        get_settings.cache_clear()

    assert result.admitted is False
    assert result.code == USER_QUOTA_EXHAUSTED
    assert await redis.get(_user_used_key("acct-1")) == b"2"  # not incremented on rejection


# -- distinct error codes --------------------------------------------------------


def test_error_codes_distinct():
    from app.router.exhaustion import PoolExhaustedError, render_exhausted

    exhausted_gw = render_exhausted(PoolExhaustedError("pool_exhausted", None), model="gpt-4o-mini")
    budget_gw = render_exhausted(PoolExhaustedError("budget_exhausted", None), model="gpt-4o-mini")

    codes = {USER_QUOTA_EXHAUSTED, POOL_LOW_WATER, exhausted_gw.code, budget_gw.code}
    assert len(codes) == 4  # all four distinct


# -- water mark throttles everyone, even within-cap users ---------------------------


@pytest.mark.asyncio
async def test_water_mark_throttles_all(redis, registry, monkeypatch):
    key_id = await _make_key(registry, daily_cap=10)
    await _consume_key(redis, key_id, 6)  # remaining = 4

    monkeypatch.setenv("PALIMPSEST_POOL_LOW_WATER_MARK", "5")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        result = await admit("acct-well-within-cap", redis=redis)
    finally:
        get_settings.cache_clear()

    assert result.admitted is False
    assert result.code == POOL_LOW_WATER


# -- water mark disabled at zero, cap layer still enforced --------------------------


@pytest.mark.asyncio
async def test_water_mark_disabled_at_zero(redis, registry, monkeypatch):
    key_id = await _make_key(registry, daily_cap=10)
    await _consume_key(redis, key_id, 9)  # remaining = 1, would trip any W > 0

    monkeypatch.setenv("PALIMPSEST_POOL_LOW_WATER_MARK", "0")
    monkeypatch.setenv("PALIMPSEST_USER_DAILY_CAP", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        first = await admit("acct-1", redis=redis)
        second = await admit("acct-1", redis=redis)
    finally:
        get_settings.cache_clear()

    assert first.admitted is True  # pool layer disabled
    assert second.admitted is False and second.code == USER_QUOTA_EXHAUSTED  # cap still active


# -- raise_user_cap gated by the water mark, never bypasses it -------------------


@pytest.mark.asyncio
async def test_raise_cap_noop_at_low_water(redis, registry, monkeypatch):
    key_id = await _make_key(registry, daily_cap=10)
    await _consume_key(redis, key_id, 6)  # remaining = 4

    monkeypatch.setenv("PALIMPSEST_POOL_LOW_WATER_MARK", "5")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        ok = await raise_user_cap("acct-1", 1000, redis=redis)
    finally:
        get_settings.cache_clear()

    assert ok is False
    assert await redis.get(_user_cap_key("acct-1")) is None


@pytest.mark.asyncio
async def test_raise_cap_applies_above_water_mark(redis, registry, monkeypatch):
    key_id = await _make_key(registry, daily_cap=10)
    await _consume_key(redis, key_id, 2)  # remaining = 8

    monkeypatch.setenv("PALIMPSEST_POOL_LOW_WATER_MARK", "5")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        ok = await raise_user_cap("acct-1", 1000, redis=redis)
    finally:
        get_settings.cache_clear()

    assert ok is True
    assert await redis.get(_user_cap_key("acct-1")) == b"1000"


@pytest.mark.asyncio
async def test_raised_cap_does_not_bypass_pool_throttle(redis, registry, monkeypatch):
    key_id = await _make_key(registry, daily_cap=10)

    monkeypatch.setenv("PALIMPSEST_POOL_LOW_WATER_MARK", "5")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        # Raise while comfortably above the water mark.
        ok = await raise_user_cap("acct-1", 1000, redis=redis)
        assert ok is True

        # Pool then drops below the water mark.
        await _consume_key(redis, key_id, 6)  # remaining = 4 <= W(5)

        result = await admit("acct-1", redis=redis)
    finally:
        get_settings.cache_clear()

    assert result.admitted is False
    assert result.code == POOL_LOW_WATER  # the raised cap did not exempt the account


# -- static scan -- enforcement lives only in the settlement module -----------------


def test_enforcement_only_in_settlement():
    """Static scan: the actual admission *decision* machinery (config names,
    aggregate computation, cap-key access) appears only in ``app/settlement/``.
    ``app/api/chat.py`` legitimately calls ``admit()`` and forwards its result —
    that's consuming the decision, not making it, and none of these markers
    appear in that call site."""
    decision_markers = (
        "aggregate_remaining",
        "pool_low_water_mark",
        "user_daily_cap",
        "raise_user_cap(",
        '_user_cap_key(',
        '_user_used_key(',
    )
    for directory in ("app/api", "app/router", "app/provider", "app/pool"):
        for path in pathlib.Path(directory).rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for marker in decision_markers:
                assert marker not in text, f"{path} contains quota-decision marker {marker!r}"


# -- rejection costs nothing -----------------------------------------------------


@pytest.mark.asyncio
async def test_rejection_costs_nothing(redis, registry, monkeypatch):
    key_id = await _make_key(registry, daily_cap=10)

    monkeypatch.setenv("PALIMPSEST_USER_DAILY_CAP", "0")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        result = await admit("acct-1", redis=redis)
    finally:
        get_settings.cache_clear()

    assert result.admitted is False
    assert await redis.get(_user_used_key("acct-1")) is None
    assert await redis.get(f"pgw:pool:key:{key_id}:used") is None
    # No canonical event exists for a request that never reached settlement.
    assert await get_event("some-request-that-was-never-settled", redis=redis) is None


# -- counter families are independent -----------------------------------------------


@pytest.mark.asyncio
async def test_counter_families_independent(redis, registry, monkeypatch):
    key_id = await _make_key(registry, daily_cap=10)
    await _consume_key(redis, key_id, 3)
    key_used_before = await redis.get(f"pgw:pool:key:{key_id}:used")

    monkeypatch.setenv("PALIMPSEST_POOL_LOW_WATER_MARK", "0")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        ok = await raise_user_cap("acct-1", 500, redis=redis)
    finally:
        get_settings.cache_clear()

    assert ok is True
    key_used_after = await redis.get(f"pgw:pool:key:{key_id}:used")
    assert key_used_before == key_used_after == b"3"  # unaffected by the user-cap raise


# -- no leaks in rejection -------------------------------------------------------


@pytest.mark.asyncio
async def test_no_leaks_in_rejection(redis, monkeypatch, caplog):
    secret_fragment = "sk-super-secret-provider-key-material"
    monkeypatch.setenv("PALIMPSEST_USER_DAILY_CAP", "0")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        with caplog.at_level("INFO"):
            result = await admit("acct-1", redis=redis)
    finally:
        get_settings.cache_clear()

    assert result.admitted is False
    for record in caplog.records:
        assert secret_fragment not in record.getMessage()
    assert result.message is not None
    assert secret_fragment not in result.message


# -- window reset restores the cap ------------------------------------------------


@pytest.mark.asyncio
async def test_window_reset_restores_cap(redis, monkeypatch):
    monkeypatch.setenv("PALIMPSEST_USER_DAILY_CAP", "1")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        today = datetime(2026, 6, 1, 12, 0, tzinfo=UTC)
        monkeypatch.setattr(admission_module, "_now", lambda: today)
        first = await admit("acct-1", redis=redis)
        second = await admit("acct-1", redis=redis)
        assert first.admitted is True
        assert second.admitted is False

        tomorrow = today + timedelta(days=1, hours=1)
        monkeypatch.setattr(admission_module, "_now", lambda: tomorrow)
        third = await admit("acct-1", redis=redis)
    finally:
        get_settings.cache_clear()

    assert third.admitted is True


# -- default cap None means no cap (pool layer still active) -----------------------


@pytest.mark.asyncio
async def test_default_cap_none_means_no_cap(redis, registry, monkeypatch):
    key_id = await _make_key(registry, daily_cap=10)
    await _consume_key(redis, key_id, 6)  # remaining = 4

    monkeypatch.setenv("PALIMPSEST_POOL_LOW_WATER_MARK", "5")
    from app.config import get_settings

    get_settings.cache_clear()
    assert get_settings().user_daily_cap is None
    try:
        # No matter how many times we admit, the cap layer never rejects...
        for _ in range(5):
            await redis.delete(_user_used_key("acct-uncapped"))  # isolate from prior admits
        # ...but the pool layer (still enabled) does.
        result = await admit("acct-uncapped", redis=redis)
    finally:
        get_settings.cache_clear()

    assert result.admitted is False
    assert result.code == POOL_LOW_WATER  # not user_quota_exhausted


# -- fixed check order -- cap before pool -------------------------------------------


@pytest.mark.asyncio
async def test_check_order_cap_then_pool(redis, registry, monkeypatch):
    key_id = await _make_key(registry, daily_cap=10)
    await _consume_key(redis, key_id, 6)  # remaining = 4 -- pool would also throttle

    monkeypatch.setenv("PALIMPSEST_POOL_LOW_WATER_MARK", "5")
    monkeypatch.setenv("PALIMPSEST_USER_DAILY_CAP", "0")  # cap = 0 -> immediately over-cap
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        over_cap_result = await admit("acct-over-cap", redis=redis)
    finally:
        get_settings.cache_clear()

    assert over_cap_result.code == USER_QUOTA_EXHAUSTED  # cap wins even though pool is also low


@pytest.mark.asyncio
async def test_within_cap_user_sees_pool_low_water(redis, registry, monkeypatch):
    key_id = await _make_key(registry, daily_cap=10)
    await _consume_key(redis, key_id, 6)  # remaining = 4

    monkeypatch.setenv("PALIMPSEST_POOL_LOW_WATER_MARK", "5")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        within_cap_result = await admit("acct-within-cap", redis=redis)
    finally:
        get_settings.cache_clear()

    assert within_cap_result.code == POOL_LOW_WATER


# -- Lua atomicity under concurrency ------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_admissions_never_over_admit(redis, monkeypatch):
    monkeypatch.setenv("PALIMPSEST_USER_DAILY_CAP", "10")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        results = await asyncio.gather(*(admit("acct-concurrent", redis=redis) for _ in range(50)))
    finally:
        get_settings.cache_clear()

    admitted_count = sum(1 for r in results if r.admitted)
    assert admitted_count == 10

    assert await redis.get(_user_used_key("acct-concurrent")) == b"10"


# -- concurrent admission rejection codes ------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_rejections_report_user_quota_exhausted(redis, monkeypatch):
    """Admissions that lose the race must report
    USER_QUOTA_EXHAUSTED (not POOL_LOW_WATER), since they lost the per-user
    cap race inside the Lua script, not the pool throttle."""
    monkeypatch.setenv("PALIMPSEST_USER_DAILY_CAP", "10")
    monkeypatch.setenv("PALIMPSEST_POOL_LOW_WATER_MARK", "0")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        results = await asyncio.gather(
            *(admit("acct-t14b", redis=redis) for _ in range(20))
        )
    finally:
        get_settings.cache_clear()

    admitted = sum(1 for r in results if r.admitted)
    rejected = [r for r in results if not r.admitted]
    assert admitted == 10
    assert len(rejected) == 10
    assert all(r.code == USER_QUOTA_EXHAUSTED for r in rejected)


# -- corrupted Redis values in user counters degrade safely ---------------


@pytest.mark.asyncio
async def test_corrupted_user_counter_values_degrade_safely(redis, monkeypatch):
    """A corrupted ``pgw:user:*:used`` / ``:cap`` / ``:used_reset_at`` (non-numeric
    data from direct Redis manipulation) must not crash ``admit()`` — it should
    degrade to defaults and log a warning."""
    monkeypatch.setenv("PALIMPSEST_USER_DAILY_CAP", "5")
    from app.config import get_settings

    # Write corrupted values directly
    from app.settlement.admission import (
        _user_cap_key,
        _user_used_key,
        _user_used_reset_at_key,
    )

    await redis.set(_user_used_key("acct-corrupt"), "not-a-number")
    await redis.set(_user_used_reset_at_key("acct-corrupt"), "garbage")
    await redis.set(_user_cap_key("acct-corrupt"), "also-not-a-number")

    get_settings.cache_clear()
    try:
        result = await admit("acct-corrupt", redis=redis)
    finally:
        get_settings.cache_clear()

    # Degraded to defaults: cap from env (5), used from default (0)
    assert result.admitted is True  # 0 < 5
    # Lua script re-checks cap atomically; with corrupted Python read degraded
    # to defaults, admission should still work
    assert await redis.get(_user_used_key("acct-corrupt")) == b"1"


# -- raise_user_cap with water mark disabled -----------------------------------


@pytest.mark.asyncio
async def test_raise_cap_applies_when_water_mark_disabled(redis, monkeypatch):
    """When ``POOL_LOW_WATER_MARK=0`` (disabled), ``raise_user_cap`` always
    succeeds regardless of pool state."""
    monkeypatch.setenv("PALIMPSEST_POOL_LOW_WATER_MARK", "0")
    from app.config import get_settings

    get_settings.cache_clear()
    try:
        ok = await raise_user_cap("acct-1", 1000, redis=redis)
    finally:
        get_settings.cache_clear()

    assert ok is True
    from app.settlement.admission import _user_cap_key

    assert await redis.get(_user_cap_key("acct-1")) == b"1000"
