"""Operator analytics: stats, costs, alerts, audit, export, system, metrics."""
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore
from app.usage.stats import LATENCY_EDGES, percentile
from app.usage.store import UsageStore


@pytest.fixture
async def redis():
    return FakeRedis(decode_responses=True)


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def op(redis):
    result = await OperatorStore(redis).create_key()
    return {"Authorization": f"Bearer {result['key']}"}


async def _record(redis, **kw):
    base = dict(request_id="r", account_id="acc1", session_id=None, model="gpt-4o-mini",
                status_code=200, outcome="success", latency_ms=120, stream=False, attempts=1,
                prompt_tokens=1000, completion_tokens=500, provider="openai", key_id="k1")
    base.update(kw)
    await UsageStore(redis).record_request(**base)


def test_percentile_interpolates():
    buckets = [0] * (len(LATENCY_EDGES) + 1)
    buckets[2] = 10  # 100-200 ms
    assert 100 <= percentile(buckets, 0.5) <= 200
    assert percentile([0] * 3, 0.5) is None


@pytest.mark.asyncio
async def test_stats_cover_errors_latency_and_keys(client, redis, op):
    await _record(redis)
    await _record(redis, latency_ms=3000, attempts=2, failures=[{"status_code": 429, "classification": "transient", "key_id": "k0"}])
    await _record(redis, status_code=503, outcome="exhausted_503", prompt_tokens=None, completion_tokens=None,
                  key_id=None, failures=[{"status_code": 500, "classification": "outage", "key_id": "k1"}])
    data = (await client.get("/operator/v1/stats?days=1", headers=op)).json()
    t = data["totals"]
    assert (t["requests"], t["ok"], t["errors_5xx"], t["exhausted"], t["failovers"]) == (3, 2, 1, 1, 1)
    assert t["p95_ms"] >= t["p50_ms"] > 0
    assert data["error_classes"] == {"transient": 1, "outage": 1}
    assert data["providers"]["openai"]["requests"] == 3
    keys = {k["key_id"]: k for k in data["keys"]}
    assert keys["k1"]["requests"] == 2 and keys["k1"]["failed_tries"] == 1
    assert keys["k0"]["failed_tries"] == 1
    hourly = (await client.get("/operator/v1/stats/hourly?hours=2", headers=op)).json()
    assert hourly["hourly"][-1]["requests"] == 3


@pytest.mark.asyncio
async def test_costs_and_budget(client, redis, op):
    await _record(redis)
    await _record(redis, model="mystery")
    resp = await client.put("/operator/v1/pricing", headers=op, json={
        "currency": "USD", "monthly_budget": 10, "prices": {"gpt-4o-mini": {"input": 1, "output": 2}}})
    assert resp.status_code == 200
    data = (await client.get("/operator/v1/costs?days=1", headers=op)).json()
    # 1000*1 + 500*2 per 1M tokens
    assert data["total"] == pytest.approx(0.002)
    assert data["unpriced"] == [{"model": "mystery", "tokens": 1500}]
    assert data["month"]["budget"] == 10
    overview = (await client.get("/operator/v1/overview?days=1", headers=op)).json()
    assert overview["cost"]["total"] == pytest.approx(0.002)
    assert overview["totals"]["requests"] == 2


@pytest.mark.asyncio
async def test_alerts_audit_export_system_metrics(client, redis, op):
    resp = await client.put("/operator/v1/alerts/rules", headers=op, json={"webhook_url": "ftp://x"})
    assert resp.status_code == 422
    assert (await client.put("/operator/v1/alerts/rules", headers=op, json={"error_rate_min_requests": 1})).status_code == 200
    await _record(redis, status_code=500, outcome="error_5xx")
    alerts = (await client.get("/operator/v1/alerts", headers=op)).json()["data"]
    assert any(a["id"] == "error-rate" for a in alerts)

    audit = (await client.get("/operator/v1/audit", headers=op)).json()["data"]
    assert audit[0]["action"] == "alerts.update"

    csv_resp = await client.get("/operator/v1/export/requests.csv", headers=op)
    assert csv_resp.status_code == 200 and "request_id" in csv_resp.text.splitlines()[0]
    assert (await client.get("/operator/v1/export/usage.csv?days=1", headers=op)).status_code == 200
    assert (await client.get("/operator/v1/export/audit.csv", headers=op)).status_code == 200

    system = (await client.get("/operator/v1/system", headers=op)).json()
    assert system["redis"]["reachable"] is True
    metrics = await client.get("/operator/v1/metrics", headers=op)
    assert "palimpsest_requests_today 1" in metrics.text
    assert (await client.get("/operator/v1/metrics")).status_code == 401


@pytest.mark.asyncio
async def test_account_detail_and_audit_of_actions(client, redis, op):
    body = (await client.post("/v1/accounts", json={})).json()
    await _record(redis, account_id=body["id"])
    detail = (await client.get(f"/operator/v1/accounts/{body['id']}/stats?days=3", headers=op)).json()
    assert detail["totals"]["requests"] == 1 and len(detail["daily"]) == 3
    assert (await client.get("/operator/v1/accounts/nope/stats", headers=op)).status_code == 404
    await client.post(f"/operator/v1/accounts/{body['id']}/disable", headers=op)
    actions = [a["action"] for a in (await client.get("/operator/v1/audit", headers=op)).json()["data"]]
    assert "account.disable" in actions
