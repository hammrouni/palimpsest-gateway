"""Live feed: events appear only while the page is open, in order, without content."""
import json

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

import app.router.failover as failover_module
from app.config import get_settings
from app.main import app
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.provider.adapter import AttemptResult, ProviderAttemptError
from app.router import live
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


async def _chat(client, model="gpt-4o", text="SECRET-PROMPT-TEXT"):
    account = await client.post("/v1/accounts", json={})
    api_key = account.json()["keys"][0]["key"]
    return await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": model, "messages": [{"role": "user", "content": text}]},
    )


async def _poll(client, op, after=None):
    path = "/operator/v1/live" + (f"?after={after}" if after else "")
    return (await client.get(path, headers=op)).json()


@pytest.mark.asyncio
async def test_the_feed_needs_an_operator_key(client):
    assert (await client.get("/operator/v1/live")).status_code == 401


@pytest.mark.asyncio
async def test_nothing_is_recorded_while_nobody_watches(client, redis, monkeypatch):
    await _key(redis, Provider.OPENAI, ["gpt-4o"], "o")

    async def fake(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    assert (await _chat(client)).status_code == 200
    await drain()

    assert await redis.xlen("pgw:live:events") == 0


@pytest.mark.asyncio
async def test_a_request_is_followed_phase_by_phase(client, redis, op, monkeypatch):
    key = await _key(redis, Provider.OPENAI, ["gpt-4o"], "o")

    async def fake(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    first = await _poll(client, op)  # opens the feed
    assert first["events"] == []

    assert (await _chat(client)).status_code == 200
    await drain()
    data = await _poll(client, op, first["cursor"])

    kinds = [e["kind"] for e in data["events"]]
    assert kinds == ["request", "attempt", "result", "done"]
    request, attempt, result, done = data["events"]
    assert len({e["rid"] for e in data["events"]}) == 1
    assert request["requested"] == "gpt-4o" and request["routed_by"] is None
    assert attempt["key_id"] == key.id and attempt["provider"] == "openai" and attempt["n"] == 1
    assert result["ok"] is True and result["status_code"] is None and result["ms"] >= 0
    assert done["status_code"] == 200 and done["outcome"] == "success" and done["provider"] == "openai"
    assert done["latency_ms"] >= 0 and "id" in done and data["cursor"] == done["id"]
    # Metadata only: the prompt never reaches the feed.
    assert "SECRET-PROMPT-TEXT" not in json.dumps(data)


@pytest.mark.asyncio
async def test_the_cursor_returns_only_what_is_new(client, redis, op):
    await live.read(redis, None)
    for i in range(3):
        await live.emit(redis, {"rid": f"r{i}", "kind": "request"})

    first = await _poll(client, op)
    assert [e["rid"] for e in first["events"]] == ["r0", "r1", "r2"]
    assert (await _poll(client, op, first["cursor"]))["events"] == []
    await live.emit(redis, {"rid": "r3", "kind": "request"})
    assert [e["rid"] for e in (await _poll(client, op, first["cursor"]))["events"]] == ["r3"]


@pytest.mark.asyncio
async def test_a_failed_attempt_and_a_move_to_another_model_are_shown(client, redis, op, monkeypatch):
    await _key(redis, Provider.OPENAI, ["gpt-4o", "gpt-4o-mini"], "o")

    async def fake(reg, key_id, model, messages, **params):
        if model == "gpt-4o":
            error = ProviderAttemptError(400, "x", "does not support image input", Provider.OPENAI)
            return AttemptResult(ok=False, error=error)
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    cursor = (await _poll(client, op))["cursor"]

    assert (await _chat(client, "palimpsest/supermode")).status_code == 200
    await drain()
    events = (await _poll(client, op, cursor))["events"]

    assert [e["kind"] for e in events] == [
        "request", "attempt", "result", "hop", "attempt", "result", "done",
    ]
    assert events[0]["routed_by"] == "supermode" and events[0]["model"] == "gpt-4o"
    failed = events[2]
    assert failed["ok"] is False and failed["status_code"] == 400 and failed["classification"] == "fatal"
    assert events[3] == {**events[3], "from": "gpt-4o", "to": "gpt-4o-mini", "cause": "vision"}
    assert events[5]["ok"] is True and events[6]["model"] == "gpt-4o-mini"


@pytest.mark.asyncio
async def test_a_refused_request_still_ends_with_a_done_event(client, redis, op):
    cursor = (await _poll(client, op))["cursor"]

    assert (await _chat(client, "no-such-model")).status_code in (429, 503)
    await drain()
    events = (await _poll(client, op, cursor))["events"]

    assert [e["kind"] for e in events] == ["request", "done"]
    assert events[1]["status_code"] in (429, 503) and events[1]["attempts"] == 0


@pytest.mark.asyncio
async def test_the_feed_is_capped_and_a_bad_cursor_is_refused(client, redis, op):
    for i in range(live.MAX_EVENTS + 20):
        await live.emit(redis, {"rid": str(i), "kind": "request"})

    assert await redis.xlen("pgw:live:events") == live.MAX_EVENTS
    assert (await client.get("/operator/v1/live?after=nope", headers=op)).status_code == 422


@pytest.mark.asyncio
async def test_a_streamed_request_ends_with_a_done_event(client, redis, op, chat_provider_mock):
    cursor = (await _poll(client, op))["cursor"]
    account = await client.post("/v1/accounts", json={})
    api_key = account.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200 and "[DONE]" in resp.text
    await drain()
    events = (await _poll(client, op, cursor))["events"]

    assert [e["kind"] for e in events] == ["request", "attempt", "result", "done"]
    assert events[0]["stream"] is True and events[3]["stream"] is True
    assert events[3]["status_code"] == 200 and events[3]["outcome"] == "success"


async def _calls(client, op, query=""):
    return (await client.get("/operator/v1/calls" + query, headers=op)).json()["data"]


@pytest.mark.asyncio
async def test_the_call_history_needs_an_operator_key(client):
    assert (await client.get("/operator/v1/calls")).status_code == 401


@pytest.mark.asyncio
async def test_every_call_is_kept_with_the_keys_and_models_it_tried(client, redis, op, monkeypatch):
    a = await _key(redis, Provider.OPENAI, ["gpt-4o", "gpt-4o-mini"], "o")

    async def fake(reg, key_id, model, messages, **params):
        if model == "gpt-4o":
            error = ProviderAttemptError(400, "x", "does not support image input", Provider.OPENAI)
            return AttemptResult(ok=False, error=error)
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    # Nobody is watching the live page: the history is kept all the same.
    assert (await _chat(client, "palimpsest/supermode", "SECRET-PROMPT-TEXT")).status_code == 200
    await drain()

    [call] = await _calls(client, op)
    assert call["requested"] == "palimpsest/supermode" and call["routed_by"] == "supermode"
    assert call["model"] == "gpt-4o-mini" and call["status_code"] == 200 and call["outcome"] == "success"
    assert call["t1"] >= call["t0"] and call["latency_ms"] >= 0 and call["stream"] is False
    first, second = call["attempts"]
    assert (first["key_id"], first["model"], first["ok"], first["status_code"]) == (a.id, "gpt-4o", False, 400)
    assert first["classification"] == "fatal" and first["provider"] == "openai" and first["ms"] >= 0
    assert (second["key_id"], second["model"], second["ok"]) == (a.id, "gpt-4o-mini", True)
    assert call["hops"] == [{"from": "gpt-4o", "to": "gpt-4o-mini", "cause": "vision"}]
    assert "SECRET-PROMPT-TEXT" not in json.dumps(call)


@pytest.mark.asyncio
async def test_a_refused_call_is_kept_too_and_the_list_can_be_filtered(client, redis, op, monkeypatch):
    await _key(redis, Provider.OPENAI, ["gpt-4o"], "o")

    async def fake(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake)
    await _chat(client, "no-such-model")
    await _chat(client, "gpt-4o")
    await drain()

    data = await _calls(client, op)
    assert [c["requested"] for c in data] == ["gpt-4o", "no-such-model"]  # newest first
    assert data[1]["attempts"] == [] and data[1]["status_code"] in (429, 503)
    assert len(await _calls(client, op, "?limit=1")) == 1
    assert await _calls(client, op, "?account_id=nobody") == []
    assert len(await _calls(client, op, f"?account_id={data[0]['account_id']}")) == 1


@pytest.mark.asyncio
async def test_the_history_keeps_only_as_many_calls_as_the_recent_requests_do(redis, monkeypatch):
    from app.usage import calls

    monkeypatch.setattr(get_settings(), "usage_recent_max", 10)
    for i in range(25):
        await calls.record(redis, {"rid": str(i), "account_id": "a"})

    kept = await calls.recent(redis, 1000)
    assert len(kept) == 10 and kept[0]["rid"] == "24"
