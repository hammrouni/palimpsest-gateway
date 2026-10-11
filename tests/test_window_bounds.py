"""A key's quota window ends at its announced reset time, even for the very first window."""
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis.aioredis import FakeRedis

from app.pool import tokens
from app.pool.models import KeyCreate, Provider, window_bounds
from app.pool.registry import PoolRegistry

NOW = datetime(2026, 10, 6, 15, 0, tzinfo=UTC)
DAY = 86400


def test_a_future_reset_ends_the_current_window():
    anchor = NOW + timedelta(hours=9)  # "renews in 9h"

    start, end = window_bounds(anchor, 24, NOW)

    assert end == anchor.timestamp()  # counters restart at the announced time...
    assert start == anchor.timestamp() - DAY  # ...not a whole period later


def test_a_past_anchor_behaves_as_before():
    anchor = NOW - timedelta(hours=30)

    start, end = window_bounds(anchor, 24, NOW)

    assert start == anchor.timestamp() + DAY
    assert end == start + DAY


def test_the_window_flips_exactly_at_the_reset_time():
    anchor = NOW + timedelta(hours=9)

    before = window_bounds(anchor, 24, anchor - timedelta(seconds=1))
    at = window_bounds(anchor, 24, anchor)

    assert before[1] == at[0] == anchor.timestamp()


@pytest.mark.asyncio
async def test_token_counter_restarts_at_the_announced_reset_not_a_day_later(monkeypatch):
    import os

    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("PALIMPSEST_GATE_MASTER_KEY", Fernet.generate_key().decode())
    redis = FakeRedis()
    anchor = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=9)
    record = await PoolRegistry(redis).create(
        KeyCreate(provider=Provider.OPENAI, models=["m"], key="sk-window", reset_at=anchor)
    )

    await tokens.add_tokens(redis, record, 500)
    assert await tokens.tokens_used(redis, record) == 500

    just_after = anchor + timedelta(minutes=1)
    assert await tokens.tokens_used(redis, record, now=just_after) == 0  # renewed on time
    just_before = anchor - timedelta(minutes=1)
    assert await tokens.tokens_used(redis, record, now=just_before) == 500
