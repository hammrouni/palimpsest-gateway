"""Usage and requests carry the provider, so the dashboard can group by it."""

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore
from app.usage.store import UsageStore, drain


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


async def _chat(client, model="gpt-4o-mini"):
    body = (await client.post("/v1/accounts", json={})).json()
    headers = {"Authorization": f"Bearer {body['keys'][0]['key']}"}
    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )
    await drain()
    return resp


@pytest.mark.asyncio
async def test_a_served_request_is_recorded_under_the_provider_that_served_it(
    client, redis, chat_provider_mock, operator_headers
):
    assert (await _chat(client)).status_code == 200

    (row,) = (await client.get("/operator/v1/usage?days=1", headers=operator_headers)).json()["rows"]
    assert row["provider"] == "openai"
    assert (row["requests"], row["total_tokens"]) == (1, 7)

    (record,) = (await client.get("/operator/v1/requests", headers=operator_headers)).json()["data"]
    assert record["provider"] == "openai"


@pytest.mark.asyncio
async def test_a_request_nobody_served_has_no_provider(
    client, redis, chat_provider_mock, operator_headers
):
    assert (await _chat(client, model="unserved")).status_code == 429

    (record,) = (await client.get("/operator/v1/requests", headers=operator_headers)).json()["data"]
    assert record["provider"] is None


@pytest.mark.asyncio
async def test_two_providers_stay_separate_rows_for_the_same_model(redis):
    store = UsageStore(redis)
    for provider in ("openai", "openrouter", "openrouter"):
        await store.record_request(
            request_id="r", account_id="acct", session_id=None, model="shared-model",
            status_code=200, outcome="success", latency_ms=1, stream=False, attempts=1,
            prompt_tokens=10, completion_tokens=5, provider=provider,
        )

    rows = {r["provider"]: r for r in (await store.usage(1))["rows"]}

    assert set(rows) == {"openai", "openrouter"}
    assert rows["openrouter"]["requests"] == 2 and rows["openrouter"]["total_tokens"] == 30
    assert rows["openai"]["requests"] == 1


@pytest.mark.asyncio
async def test_dashboard_bundle_offers_grouping_on_each_page(client):
    js = (await client.get("/dashboard/app.js")).text

    for needle in (
        "function appendGrouped(",
        "'By provider'",
        "'By user and provider'",
        "const REQUEST_GROUPS",
        "const KEY_GROUPS",
        "labelled('Group by', groupSelect)",
        "labelled('Group keys', groupBy)",
    ):
        assert needle in js, needle
