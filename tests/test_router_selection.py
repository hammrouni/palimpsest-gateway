"""Quota-aware key selection."""
import os

import pytest
from fakeredis.aioredis import FakeRedis

from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.router.selection import Candidate, NoCandidateError, select, select_candidates


@pytest.fixture(autouse=True)
def _master_key():
    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        os.environ["PALIMPSEST_GATE_MASTER_KEY"] = Fernet.generate_key().decode()


@pytest.fixture
async def registry():
    return PoolRegistry(FakeRedis())


async def _add_key(
    registry: PoolRegistry,
    *,
    key: str,
    provider: Provider = Provider.OPENAI,
    models: list[str] | None = None,
    daily_cap: float | None = None,
) -> str:
    record = await registry.create(
        KeyCreate(
            provider=provider,
            models=models or ["gpt-4o-mini"],
            key=key,
            daily_cap=daily_cap,
        )
    )
    return record.id


async def _set_used(registry: PoolRegistry, key_id: str, used: int) -> None:
    await registry.redis.set(f"pgw:pool:key:{key_id}:used", str(used))


# -- remaining-cap descending, no counters -----------------------------------


@pytest.mark.asyncio
async def test_orders_by_remaining_cap_descending(registry: PoolRegistry):
    id_100 = await _add_key(registry, key="k100", daily_cap=100)
    id_50 = await _add_key(registry, key="k50", daily_cap=50)
    id_10 = await _add_key(registry, key="k10", daily_cap=10)

    candidates = await select_candidates(registry, model="gpt-4o-mini")

    assert [c.key_id for c in candidates] == [id_100, id_50, id_10]
    assert [c.remaining_cap for c in candidates] == [100, 50, 10]


# -- used counters honored; tie-break by key_id asc ---------------------------


@pytest.mark.asyncio
async def test_used_counters_honored_and_ties_broken_by_key_id(registry: PoolRegistry):
    id_a = await _add_key(registry, key="ka", daily_cap=100)
    id_b = await _add_key(registry, key="kb", daily_cap=50)
    id_c = await _add_key(registry, key="kc", daily_cap=10)

    await _set_used(registry, id_a, 80)  # remaining 20
    await _set_used(registry, id_b, 40)  # remaining 10
    await _set_used(registry, id_c, 0)  # remaining 10

    candidates = await select_candidates(registry, model="gpt-4o-mini")

    tied = sorted([id_b, id_c])
    assert [c.key_id for c in candidates] == [id_a, *tied]
    assert [c.remaining_cap for c in candidates] == [20, 10, 10]


# -- zero-remaining dropped; uncapped survives as overflow --------------------


@pytest.mark.asyncio
async def test_zero_remaining_dropped_uncapped_survives(registry: PoolRegistry):
    exhausted = await _add_key(registry, key="k-exhausted", daily_cap=10)
    await _set_used(registry, exhausted, 10)
    uncapped = await _add_key(registry, key="k-uncapped", daily_cap=None)

    candidates = await select_candidates(registry, model="gpt-4o-mini")

    assert exhausted not in [c.key_id for c in candidates]
    assert [c.key_id for c in candidates] == [uncapped]
    assert candidates[0].remaining_cap is None


# -- capped-with-remaining always precede uncapped ----------------------------


@pytest.mark.asyncio
async def test_capped_precedes_uncapped_regardless_of_provider(registry: PoolRegistry):
    uncapped_a = await _add_key(registry, key="u-a", provider=Provider.ANTHROPIC, daily_cap=None)
    uncapped_b = await _add_key(registry, key="u-b", provider=Provider.OPENAI, daily_cap=None)
    capped = await _add_key(registry, key="c-low", provider=Provider.OPENAI, daily_cap=1)

    candidates = await select_candidates(registry, model="gpt-4o-mini")

    assert candidates[0].key_id == capped
    assert [c.key_id for c in candidates[1:]] == sorted([uncapped_a, uncapped_b])


# -- preferred_provider precedes cross-provider; falls back gracefully -------


@pytest.mark.asyncio
async def test_preferred_provider_precedes_higher_remaining_other_provider(
    registry: PoolRegistry,
):
    openai_key = await _add_key(registry, key="o1", provider=Provider.OPENAI, daily_cap=1000)
    anthropic_key = await _add_key(
        registry, key="a1", provider=Provider.ANTHROPIC, daily_cap=10
    )

    candidates = await select_candidates(
        registry, model="gpt-4o-mini", preferred_provider=Provider.ANTHROPIC
    )

    assert [c.key_id for c in candidates] == [anthropic_key, openai_key]


@pytest.mark.asyncio
async def test_preferred_provider_with_no_active_key_falls_back(registry: PoolRegistry):
    openai_key = await _add_key(registry, key="o2", provider=Provider.OPENAI, daily_cap=10)

    candidates = await select_candidates(
        registry, model="gpt-4o-mini", preferred_provider=Provider.ANTHROPIC
    )

    assert [c.key_id for c in candidates] == [openai_key]


# -- exclusion set honored; empty universe never raises -----------------------


@pytest.mark.asyncio
async def test_excluded_set_surfaces_next_key(registry: PoolRegistry):
    top = await _add_key(registry, key="top", daily_cap=100)
    second = await _add_key(registry, key="second", daily_cap=50)

    candidates = await select_candidates(
        registry, model="gpt-4o-mini", excluded=frozenset({top})
    )

    assert [c.key_id for c in candidates] == [second]


@pytest.mark.asyncio
async def test_all_excluded_returns_empty_and_select_returns_none(registry: PoolRegistry):
    id1 = await _add_key(registry, key="e1", daily_cap=100)
    id2 = await _add_key(registry, key="e2", daily_cap=50)

    candidates = await select_candidates(
        registry, model="gpt-4o-mini", excluded=frozenset({id1, id2})
    )
    assert candidates == []

    result = await select(registry, model="gpt-4o-mini", excluded=frozenset({id1, id2}))
    assert result is None


def test_no_candidate_error_message_is_provider_and_key_free():
    err = NoCandidateError("gpt-4o-mini")
    message = str(err)
    assert "gpt-4o-mini" in message
    for provider in Provider:
        assert provider.value not in message


# -- model filter --------------------------------------------------------------


@pytest.mark.asyncio
async def test_model_filter_restricts_candidates(registry: PoolRegistry):
    claude_only = await _add_key(registry, key="claude-key", models=["claude-x"])
    multi = await _add_key(registry, key="multi-key", models=["gpt-4o-mini", "claude-x"])

    gpt_candidates = await select_candidates(registry, model="gpt-4o-mini")
    assert [c.key_id for c in gpt_candidates] == [multi]

    claude_candidates = await select_candidates(registry, model="claude-x")
    assert {c.key_id for c in claude_candidates} == {claude_only, multi}


# -- key state honored; markers exclude; expiry restores ----------------------


@pytest.mark.asyncio
async def test_pool_state_honored_suspended_exhausted_cooldown(registry: PoolRegistry):
    active = await _add_key(registry, key="s-active", daily_cap=10)
    suspended = await _add_key(registry, key="s-suspended", daily_cap=20)
    exhausted = await _add_key(registry, key="s-exhausted", daily_cap=30)
    cooldown = await _add_key(registry, key="s-cooldown", daily_cap=40)

    await registry.suspend(suspended)
    await registry.mark_exhausted(exhausted)
    await registry.mark_cooldown(cooldown)

    candidates = await select_candidates(registry, model="gpt-4o-mini")
    assert [c.key_id for c in candidates] == [active]


@pytest.mark.asyncio
async def test_markers_reappear_after_ttl_expiry(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    import time as time_module

    from app.config import get_settings

    monkeypatch.setenv("PALIMPSEST_COOLDOWN_SECONDS", "2")
    get_settings.cache_clear()
    try:
        low = await _add_key(registry, key="e-low", daily_cap=10)
        high = await _add_key(registry, key="e-high", daily_cap=50)
        await registry.mark_cooldown(low)

        mid_candidates = await select_candidates(registry, model="gpt-4o-mini")
        assert [c.key_id for c in mid_candidates] == [high]

        future = time_module.time() + 100
        monkeypatch.setattr(time_module, "time", lambda: future)

        candidates = await select_candidates(registry, model="gpt-4o-mini")
        assert [c.key_id for c in candidates] == [high, low]
    finally:
        get_settings.cache_clear()


# -- Static: no retry loop in the selection module --------------------


def test_no_retry_loop_in_selection_module():
    import pathlib

    text = pathlib.Path("app/router/selection.py").read_text()
    assert "while" not in text
    for token in ("RETRY_BUDGET", "retry_budget", "num_retries"):
        assert token not in text


def test_candidate_is_frozen_dataclass():
    import dataclasses

    candidate = Candidate(key_id="x", provider=Provider.OPENAI, remaining_cap=1)
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.key_id = "y"
