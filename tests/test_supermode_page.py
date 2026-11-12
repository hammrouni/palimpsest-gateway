"""Supermode page: the statistics it records and the ranking it explains."""
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

import app.router.failover as failover_module
from app.config import get_settings
from app.main import app
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.provider.adapter import AttemptResult, ProviderAttemptError
from app.router import supermode_stats
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore
from app.usage.store import drain
from tests.conftest import _FakeBlockingResponse


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def op(redis):
    result = await OperatorStore(redis).create_key()
    return {"Authorization": f"Bearer {result['key']}"}


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    monkeypatch.setattr(get_settings(), "master_model", None)
    monkeypatch.setattr(get_settings(), "provider_master_models", {})
    monkeypatch.setattr(get_settings(), "supermode_quality", {})


async def _key(redis, provider, models, key):
    return await PoolRegistry(redis).create(KeyCreate(provider=provider, models=models, key=key))


async def _chat(client, extra=None):
    account = await client.post("/v1/accounts", json={})
    api_key = account.json()["keys"][0]["key"]
    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "palimpsest/supermode",
            "messages": [{"role": "user", "content": "hi"}],
            **(extra or {}),
        },
    )


@pytest.mark.asyncio
async def test_both_endpoints_need_an_operator_key(client):
    assert (await client.get("/operator/v1/supermode")).status_code == 401
    assert (await client.get("/operator/v1/supermode/ranking")).status_code == 401


@pytest.mark.asyncio
async def test_ranking_orders_models_and_says_why_others_were_dropped(client, redis, op):
    await _key(redis, Provider.OPENAI, ["gpt-4o", "gpt-4o-mini"], "o")
    await _key(redis, Provider.GROQ, ["deepseek-r1-distill"], "g")  # no vision, no tools

    plain = (await client.get("/operator/v1/supermode/ranking", headers=op)).json()
    ranked = [m["model"] for m in plain["models"] if m["position"]]
    assert ranked == ["deepseek-r1-distill", "gpt-4o", "gpt-4o-mini"]  # 88, 85, 72
    assert plain["dropped"] == 0

    vision = (
        await client.get("/operator/v1/supermode/ranking?vision=true&tools=true", headers=op)
    ).json()
    dropped = {m["model"]: m["dropped_because"] for m in vision["models"] if not m["position"]}
    assert dropped == {"deepseek-r1-distill": "vision"}
    assert vision["ranked"] == 2
    first = vision["models"][0]
    assert first["model"] == "gpt-4o" and first["position"] == 1
    assert first["score"] == first["base_quality"] - first["penalty"]

    long = (await client.get("/operator/v1/supermode/ranking?tokens=500000", headers=op)).json()
    assert {m["model"] for m in long["models"] if m["dropped_because"] == "context"} >= {"gpt-4o"}


@pytest.mark.asyncio
async def test_ranking_shows_a_paused_model_as_having_no_key_ready(client, redis, op):
    best = await _key(redis, Provider.ANTHROPIC, ["claude-opus-4-1"], "a")
    await _key(redis, Provider.OPENAI, ["gpt-4o"], "o")
    await PoolRegistry(redis).mark_exhausted(best.id, seconds=600, reason="test")

    rows = (await client.get("/operator/v1/supermode/ranking", headers=op)).json()["models"]

    by_model = {m["model"]: m for m in rows}
    assert by_model["claude-opus-4-1"]["dropped_because"] == "no_key_ready"
    assert by_model["claude-opus-4-1"]["keys_ready"] == 0
    assert by_model["gpt-4o"]["position"] == 1 and by_model["gpt-4o"]["keys_ready"] == 1


@pytest.mark.asyncio
async def test_a_served_request_is_counted_and_listed(client, redis, op, monkeypatch):
    await _key(redis, Provider.OPENAI, ["gpt-4o", "gpt-4o-mini"], "o")

    async def fake(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    assert (await _chat(client)).status_code == 200
    await drain()

    data = (await client.get("/operator/v1/supermode?days=1", headers=op)).json()
    stats = data["stats"]
    assert stats["totals"]["requests"] == 1 and stats["totals"]["ok"] == 1
    assert stats["totals"]["first_pick_served"] == 1 and stats["totals"]["hopped"] == 0
    assert stats["models"] == [{"model": "gpt-4o", "served": 1, "first_pick": 1}]
    assert stats["providers"] == [{"provider": "openai", "served": 1}]
    assert len(stats["accounts"]) == 1 and stats["accounts"][0]["requests"] == 1
    assert stats["daily"][-1]["requests"] == 1
    event = data["recent"][0]
    assert event["picked"] == "gpt-4o" and event["served"] == "gpt-4o" and event["hops"] == []
    assert data["config"]["fallback_models"] == get_settings().supermode_fallback_models
    assert data["config"]["bypassed"] is False


@pytest.mark.asyncio
async def test_a_move_to_another_model_is_recorded_with_its_cause(client, redis, op, monkeypatch):
    await _key(redis, Provider.OPENAI, ["gpt-4o", "gpt-4o-mini"], "o")

    async def fake(reg, key_id, model, messages, **params):
        if model == "gpt-4o":
            error = ProviderAttemptError(400, "x", "does not support image input", Provider.OPENAI)
            return AttemptResult(ok=False, error=error)
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    assert (await _chat(client)).status_code == 200
    await drain()

    data = (await client.get("/operator/v1/supermode?days=1", headers=op)).json()
    totals = data["stats"]["totals"]
    assert totals["hopped"] == 1 and totals["hops"] == 1 and totals["first_pick_served"] == 0
    assert data["stats"]["causes"]["vision"] == 1
    assert data["recent"][0]["hops"] == [{"from": "gpt-4o", "to": "gpt-4o-mini", "cause": "vision"}]
    served = {m["model"]: m for m in data["stats"]["models"]}
    assert served["gpt-4o-mini"]["served"] == 1 and served["gpt-4o"]["first_pick"] == 1


@pytest.mark.asyncio
async def test_a_request_nothing_could_serve_counts_as_no_model(client, redis, op):
    assert (await _chat(client)).status_code in (429, 503)
    await drain()

    totals = (await client.get("/operator/v1/supermode?days=1", headers=op)).json()["stats"]["totals"]
    assert totals["requests"] == 1 and totals["failed"] == 1 and totals["no_model"] == 1


@pytest.mark.asyncio
async def test_other_models_are_not_counted_as_supermode(client, redis, op, monkeypatch):
    await _key(redis, Provider.OPENAI, ["gpt-4o"], "o")

    async def fake(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    account = await client.post("/v1/accounts", json={})
    api_key = account.json()["keys"][0]["key"]
    await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o", "messages": [{"role": "user", "content": "hi"}]},
    )
    await drain()

    totals = (await client.get("/operator/v1/supermode?days=1", headers=op)).json()["stats"]["totals"]
    assert totals["requests"] == 0


@pytest.mark.asyncio
async def test_learned_key_limits_are_listed(client, redis, op):
    key = await _key(redis, Provider.GROQ, ["m"], "g")
    await redis.set(f"pgw:learn:key:{key.id}:rpm", "9")

    data = (await client.get("/operator/v1/supermode", headers=op)).json()

    assert data["learned_limits"] == [
        {"key_id": key.id, "label": key.label, "provider": "groq", "requests_per_minute": 9}
    ]


@pytest.mark.asyncio
async def test_events_are_capped_and_the_summary_spans_days(redis):
    for i in range(supermode_stats.EVENTS_MAX + 5):
        await supermode_stats.record(
            redis, {"picked": "m", "served": "m", "provider": "openai", "request_id": str(i)}
        )

    assert len(await supermode_stats.recent(redis, 1000)) == supermode_stats.EVENTS_MAX
    summary = await supermode_stats.summary(redis, 3)
    assert summary["totals"]["requests"] == supermode_stats.EVENTS_MAX + 5
    assert len(summary["daily"]) == 3 and summary["daily"][0]["requests"] == 0


@pytest.mark.asyncio
async def test_the_dashboard_serves_the_supermode_page(client):
    import pathlib

    page = await client.get("/dashboard/supermode.js")
    assert page.status_code == 200 and "javascript" in page.headers["content-type"]
    html = (await client.get("/dashboard/")).text
    assert html.index("insights.js") < html.index("supermode.js") < html.index("app.js")
    assert html.index("live.js") < html.index("livehistory.js") < html.index("app.js")
    for name in ("supermode.js", "live.js", "livehistory.js"):
        text = (await client.get(f"/dashboard/{name}")).text
        for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "console.", "eval("):
            assert sink not in text, (name, sink)
    shell = pathlib.Path("app/static/dashboard/app.js").read_text(encoding="utf-8")
    assert "id: 'supermode'" in shell and "render: renderSupermode" in shell
    assert "'supermode'" in shell[shell.index("const NAV_GROUPS") :]
