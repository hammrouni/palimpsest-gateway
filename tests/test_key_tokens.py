"""Per-key token budget: counted per reset window, refreshed when the window rolls."""
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
from app.usage.store import drain


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    import os

    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("PALIMPSEST_GATE_MASTER_KEY", Fernet.generate_key().decode())


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def operator_headers(redis):
    return {"Authorization": f"Bearer {(await OperatorStore(redis).create_key())['key']}"}


async def _record(redis, **kwargs):
    registry = PoolRegistry(redis)
    data = {"provider": Provider.OPENAI, "models": ["m"], "key": "sk-a", **kwargs}
    return await registry.create(KeyCreate(**data))


@pytest.mark.asyncio
async def test_tokens_accumulate_within_the_window(redis):
    record = await _record(redis, token_cap=1000)

    await tokens.add_tokens(redis, record, 120)
    await tokens.add_tokens(redis, record, 30)
    await tokens.add_tokens(redis, record, 0)  # nothing to add

    assert await tokens.tokens_used(redis, record) == 150


@pytest.mark.asyncio
async def test_counter_starts_again_when_the_window_rolls_over(redis):
    record = await _record(redis, token_cap=1000, reset_period_hours=24)
    now = datetime.now(UTC)
    await tokens.add_tokens(redis, record, 400, now=now)

    # The first window starts at the key's reset anchor (up to a day ahead), so two
    # full periods on is certainly the next window: the key's tokens are back.
    later = now + timedelta(hours=49)

    assert await tokens.tokens_used(redis, record, now=later) == 0
    await tokens.add_tokens(redis, record, 50, now=later)
    assert await tokens.tokens_used(redis, record, now=later) == 50
    assert await tokens.tokens_used(redis, record, now=now) == 400  # the old window is intact


@pytest.mark.asyncio
async def test_token_cap_is_stored_edited_and_cleared(client, redis, operator_headers):
    created = await client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={"provider": "openai", "models": ["m"], "key": "sk-new", "token_cap": 5000},
    )
    assert created.status_code == 201 and created.json()["token_cap"] == 5000
    key_id = created.json()["id"]

    base = f"/v1/operator/pool/keys/{key_id}"
    edited = await client.patch(base, headers=operator_headers, json={"token_cap": 9000})
    assert edited.json()["token_cap"] == 9000
    cleared = await client.patch(base, headers=operator_headers, json={"token_cap": None})
    assert cleared.json()["token_cap"] is None
    bad = await client.patch(base, headers=operator_headers, json={"token_cap": 0})
    assert bad.status_code == 422


@pytest.mark.asyncio
async def test_served_request_counts_against_the_key_and_shows_in_the_dashboard_view(
    client, redis, chat_provider_mock, operator_headers
):
    registry = PoolRegistry(redis)
    (key,) = await registry.list()
    await registry.update(key.id, KeyUpdate(token_cap=100))

    account = (await client.post("/v1/accounts", json={})).json()
    headers = {"Authorization": f"Bearer {account['keys'][0]['key']}"}
    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    await drain()

    view = (await client.get("/v1/operator/pool/utilization", headers=operator_headers)).json()
    (row,) = view["keys"]
    # The stubbed provider reports 5 prompt + 2 completion tokens.
    assert (row["token_cap"], row["tokens_used"], row["tokens_remaining"]) == (100, 7, 93)
    agg = view["aggregate"]
    assert (agg["tokens_used"], agg["token_cap_total"], agg["tokens_remaining_total"]) == (7, 100, 93)


@pytest.mark.asyncio
async def test_uncapped_key_still_reports_usage_but_no_remaining(
    client, redis, chat_provider_mock, operator_headers
):
    account = (await client.post("/v1/accounts", json={})).json()
    headers = {"Authorization": f"Bearer {account['keys'][0]['key']}"}
    await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    await drain()

    view = (await client.get("/v1/operator/pool/utilization", headers=operator_headers)).json()
    (row,) = view["keys"]
    assert row["token_cap"] is None and row["tokens_used"] == 7
    assert row["tokens_remaining"] is None
    assert view["aggregate"]["token_cap_total"] is None


@pytest.mark.asyncio
async def test_failed_requests_and_removed_keys_leave_no_tokens(
    client, redis, chat_provider_mock, operator_headers
):
    account = (await client.post("/v1/accounts", json={})).json()
    headers = {"Authorization": f"Bearer {account['keys'][0]['key']}"}
    # A model nobody serves: pool exhausted, nothing served, nothing counted.
    await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "unserved", "messages": [{"role": "user", "content": "x"}]},
    )
    await drain()
    (row,) = (await client.get("/v1/operator/pool/utilization", headers=operator_headers)).json()["keys"]
    assert row["tokens_used"] == 0

    registry = PoolRegistry(redis)
    (key,) = await registry.list()
    await tokens.add_tokens(redis, key, 10)
    await registry.remove(key.id)
    assert [k async for k in redis.scan_iter(match=tokens.counter_pattern(key.id))] == []
