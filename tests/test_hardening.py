"""Hardening: corrupt data, failure paths and error envelopes."""
import json
import logging
from datetime import UTC, datetime

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.session.store import SessionStore
from app.store.redis_store import RedisStore

_BODY = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _account(client) -> tuple[str, str]:
    resp = await client.post("/v1/accounts", json={})
    return resp.json()["keys"][0]["key"], resp.json()["id"]


# -- settlement read / settle / admission -----------------------------------------


@pytest.mark.asyncio
async def test_get_event_returns_none_on_corrupt_json(redis):
    from app.settlement.read import get_event
    from app.settlement.settle import _event_key

    await redis.hset(_event_key("rid"), "event", "{not json")
    assert await get_event("rid", redis=redis) is None


@pytest.mark.asyncio
async def test_used_counter_from_a_previous_window_reads_as_zero(redis):
    from app.pool.models import KeyCreate, Provider
    from app.pool.registry import PoolRegistry
    from app.settlement.read import remaining
    from app.settlement.settle import _used_key, _used_reset_at_key, _window_bounds

    registry = PoolRegistry(redis)
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["m"], key="k", daily_cap=10)
    )
    window_start, _ = _window_bounds(
        record.reset_at, record.reset_period_hours, datetime.now(UTC)
    )
    await redis.set(_used_key(record.id), 7)

    await redis.set(_used_reset_at_key(record.id), window_start)  # current window
    assert await remaining(record.id, redis=redis) == 3

    await redis.set(_used_reset_at_key(record.id), window_start - 86400)  # stale window
    assert await remaining(record.id, redis=redis) == 10


@pytest.mark.asyncio
async def test_settle_never_raises_on_non_redis_failure(redis, monkeypatch):
    import importlib

    from app.settlement import SettlementInput

    # ``app.settlement.settle`` the attribute is the re-exported function; want the module.
    settle_module = importlib.import_module("app.settlement.settle")

    class _Trace:
        attempts: list = []
        terminal = "success"

    def boom(_trace):
        raise TypeError("malformed trace")

    monkeypatch.setattr(settle_module, "_attribution", boom)
    result = await settle_module.settle(
        SettlementInput(
            request_id="r1", account_id="a", session_id=None, model="m",
            trace=_Trace(), status_code=200, latency_ms=1, stream=False,
        ),
        redis=redis,
    )
    assert result.settled is False


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [0, -1, True, 1.5])
async def test_raise_user_cap_rejects_non_positive_or_non_int(redis, bad):
    from app.settlement.admission import raise_user_cap

    with pytest.raises(ValueError):
        await raise_user_cap("acct", bad, redis=redis)


# -- chat route ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admission_redis_failure_is_a_logged_503(client, redis, chat_provider_mock, caplog, monkeypatch):
    import app.api.chat as chat_module

    async def broken_admit(*a, **kw):
        raise ConnectionError("redis down")

    monkeypatch.setattr(chat_module, "admit", broken_admit)
    api_key, _ = await _account(client)
    with caplog.at_level(logging.INFO, logger="palimpsest.requests"):
        resp = await client.post(
            "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=_BODY
        )
    assert resp.status_code == 503
    assert resp.json()["error"]["code"] == "service_unavailable"
    assert "x-request-id" in resp.headers
    records = [json.loads(r.message) for r in caplog.records if r.name == "palimpsest.requests"]
    assert len(records) == 1 and records[0]["outcome"] == "error_5xx"


@pytest.mark.asyncio
async def test_session_id_with_trailing_newline_is_rejected(client, redis, chat_provider_mock):
    api_key, _ = await _account(client)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "X-Session-ID": "abc\n"},
        json=_BODY,
    )
    # httpx may strip/refuse the header value; either way it must never reach a session.
    assert resp.status_code in (404, 422)


@pytest.mark.asyncio
async def test_blocking_persistence_failure_still_serves_the_completion(
    client, redis, chat_provider_mock, caplog, monkeypatch
):
    async def failing_append(self, account_id, session_id, messages):
        raise RuntimeError("persist failed")

    monkeypatch.setattr(SessionStore, "append_exchange", failing_append)
    api_key, _ = await _account(client)
    with caplog.at_level(logging.INFO, logger="palimpsest.requests"):
        resp = await client.post(
            "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=_BODY
        )
    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["content"]
    records = [json.loads(r.message) for r in caplog.records if r.name == "palimpsest.requests"]
    assert len(records) == 1
    assert records[0]["outcome"] == "success_persist_failed"


@pytest.mark.asyncio
async def test_tool_messages_keep_tool_fields_and_omit_unset_ones(client, redis, chat_provider_mock):
    api_key, _ = await _account(client)
    messages = [
        {"role": "user", "content": "weather?"},
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "w", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "sunny"},
    ]
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": messages},
    )
    assert resp.status_code == 200
    sent = chat_provider_mock.received_calls[-1]
    assert sent[1]["tool_calls"][0]["id"] == "c1"
    assert sent[2]["tool_call_id"] == "c1"
    assert "name" not in sent[0] and "tool_calls" not in sent[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("patch", [{"n": 2}, {"model": ""}])
async def test_unsupported_n_and_empty_model_are_422(client, redis, chat_provider_mock, patch):
    api_key, _ = await _account(client)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={**_BODY, **patch},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_streaming_chunks_hide_provider_identity(client, redis, chat_provider_mock):
    api_key, _ = await _account(client)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={**_BODY, "stream": True},
    )
    payloads = [
        json.loads(line[6:])
        for line in resp.text.splitlines()
        if line.startswith("data: ") and line != "data: [DONE]"
    ]
    assert payloads
    ids = {p["id"] for p in payloads}
    assert len(ids) == 1  # one stable gateway chat id for the whole stream
    assert all(p.get("model", "gpt-4o-mini") == "gpt-4o-mini" for p in payloads)
    assert all("system_fingerprint" not in p for p in payloads)


# -- app-level error envelope ----------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_route_uses_openai_envelope_with_request_id(client):
    resp = await client.get("/v1/no-such-route")
    assert resp.status_code == 404
    body = resp.json()
    assert set(body["error"]) == {"type", "message", "param", "code"}
    assert body["error"]["code"] == "not_found"
    assert resp.headers["x-request-id"]


@pytest.mark.asyncio
async def test_unhandled_exception_is_a_non_disclosing_500_envelope(redis):

    from app.main import create_app

    local = create_app()

    @local.get("/_boom")
    async def boom():
        raise RuntimeError("secret internal detail")

    local.state.store = RedisStore(redis)
    async with AsyncClient(
        transport=ASGITransport(app=local, raise_app_exceptions=False), base_url="http://test"
    ) as c:
        resp = await c.get("/_boom")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "internal_error"
    assert "secret" not in resp.text
    assert resp.headers["x-request-id"]


# -- config ----------------------------------------------------------------------


def test_invalid_persisted_overrides_are_ignored():
    from app.config import apply_persisted_overrides, get_settings

    before = get_settings().retry_budget
    applied = apply_persisted_overrides({"retry_budget": "not-a-number", "verbatim_turns_n": 0})
    assert applied == {}
    assert get_settings().retry_budget == before
