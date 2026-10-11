"""Editing a key's reset schedule must not strand the tokens it has already used."""
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.pool import tokens
from app.pool.models import KeyCreate, KeyUpdate, Provider
from app.pool.registry import PoolRegistry
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    import os

    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("PALIMPSEST_GATE_MASTER_KEY", Fernet.generate_key().decode())


@pytest.fixture
async def redis():
    return FakeRedis()


async def _key(redis, **kwargs):
    registry = PoolRegistry(redis)
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["m"], key="sk-carry", **kwargs)
    )
    return registry, record


@pytest.mark.asyncio
async def test_tokens_survive_moving_the_reset_time(redis):
    registry, record = await _key(redis)
    await tokens.add_tokens(redis, record, 1000)

    moved = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=15)
    updated = await registry.update(record.id, KeyUpdate(reset_at=moved))

    assert await tokens.tokens_used(redis, updated) == 1000
    # Nothing left stranded under the old window's name.
    names = [k.decode() async for k in redis.scan_iter(match=tokens.counter_pattern(record.id))]
    assert len(names) == 1


@pytest.mark.asyncio
async def test_tokens_survive_changing_the_window_length(redis):
    registry, record = await _key(redis)
    await tokens.add_tokens(redis, record, 250)

    updated = await registry.update(record.id, KeyUpdate(reset_period_hours=6))

    assert await tokens.tokens_used(redis, updated) == 250


@pytest.mark.asyncio
async def test_usage_added_after_the_edit_keeps_counting_on_top(redis):
    registry, record = await _key(redis)
    await tokens.add_tokens(redis, record, 100)
    moved = datetime.now(UTC).replace(microsecond=0) + timedelta(hours=7)
    updated = await registry.update(record.id, KeyUpdate(reset_at=moved))

    await tokens.add_tokens(redis, updated, 40)

    assert await tokens.tokens_used(redis, updated) == 140


@pytest.mark.asyncio
async def test_edits_that_leave_the_schedule_alone_touch_no_counters(redis):
    registry, record = await _key(redis)
    await tokens.add_tokens(redis, record, 77)

    updated = await registry.update(record.id, KeyUpdate(models=["m", "n"], label="Renamed"))

    assert await tokens.tokens_used(redis, updated) == 77


@pytest.mark.asyncio
async def test_a_schedule_edit_with_no_tokens_yet_is_a_no_op(redis):
    registry, record = await _key(redis)

    updated = await registry.update(
        record.id, KeyUpdate(reset_at=datetime.now(UTC) + timedelta(hours=3))
    )

    assert await tokens.tokens_used(redis, updated) == 0
    assert [k async for k in redis.scan_iter(match=tokens.counter_pattern(record.id))] == []


@pytest.mark.asyncio
async def test_through_the_api_the_dashboard_still_shows_the_tokens(redis):
    app.state.store = RedisStore(redis)
    headers = {"Authorization": f"Bearer {(await OperatorStore(redis).create_key())['key']}"}
    registry, record = await _key(redis, token_cap=5000)
    await tokens.add_tokens(redis, record, 1200)
    moved = (datetime.now(UTC) + timedelta(hours=11)).replace(microsecond=0)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        edit = await client.patch(
            f"/v1/operator/pool/keys/{record.id}", headers=headers, json={"reset_at": moved.isoformat()}
        )
        view = await client.get("/v1/operator/pool/utilization", headers=headers)

    assert edit.status_code == 200
    (row,) = view.json()["keys"]
    assert (row["tokens_used"], row["tokens_remaining"]) == (1200, 3800)
