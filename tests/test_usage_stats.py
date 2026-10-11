"""Operator statistics: token usage by account/model, recent requests, session tokens."""
import json

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

import app.router.failover as failover_module
from app.main import app
from app.provider.adapter import AttemptResult
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
    result = await OperatorStore(redis).create_key()
    return {"Authorization": f"Bearer {result['key']}"}


async def _new_account(client) -> tuple[str, dict]:
    body = (await client.post("/v1/accounts", json={})).json()
    return body["id"], {"Authorization": f"Bearer {body['keys'][0]['key']}"}


async def _chat(client, headers, content="hi", stream=False, session=None):
    extra = {"X-Session-ID": session} if session else {}
    resp = await client.post(
        "/v1/chat/completions",
        headers={**headers, **extra},
        json={
            "model": "gpt-4o-mini",
            "stream": stream,
            "messages": [{"role": "user", "content": content}],
        },
    )
    await drain()
    return resp


@pytest.mark.asyncio
async def test_blocking_turn_counts_tokens_per_account_and_model(
    client, redis, chat_provider_mock, operator_headers
):
    account_id, headers = await _new_account(client)
    for _ in range(2):
        assert (await _chat(client, headers)).status_code == 200

    resp = await client.get("/operator/v1/usage?days=1", headers=operator_headers)
    assert resp.status_code == 200
    (row,) = resp.json()["rows"]
    assert row["account_id"] == account_id
    assert row["model"] == "gpt-4o-mini"
    # The stubbed provider reports 5 prompt + 2 completion tokens per turn.
    assert (row["requests"], row["ok"]) == (2, 2)
    assert (row["prompt_tokens"], row["completion_tokens"], row["total_tokens"]) == (10, 4, 14)
    assert row["requests_without_usage"] == 0
    assert resp.json()["daily"][-1]["prompt_tokens"] == 10


@pytest.mark.asyncio
async def test_stream_without_provider_usage_is_counted_as_unknown(
    client, redis, chat_provider_mock, operator_headers
):
    _, headers = await _new_account(client)
    assert (await _chat(client, headers, stream=True)).status_code == 200

    (row,) = (await client.get("/operator/v1/usage", headers=operator_headers)).json()["rows"]
    assert row["requests"] == 1
    assert row["requests_without_usage"] == 1
    assert row["total_tokens"] == 0  # never guessed


@pytest.mark.asyncio
async def test_stream_usage_chunk_is_counted_but_not_forwarded(
    client, redis, chat_provider_mock, operator_headers, monkeypatch
):
    class _Usage:
        total_tokens, prompt_tokens, completion_tokens = 30, 20, 10

    class _UsageOnlyChunk:
        choices: list = []
        usage = _Usage()

        def model_dump(self):
            return {"id": "x", "choices": [], "usage": {"total_tokens": 30}}

    async def stream():
        from tests.conftest import _FakeChunk

        yield _FakeChunk(content="hello")
        yield _FakeChunk(finish_reason="stop")
        yield _UsageOnlyChunk()

    async def fake_attempt(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=stream())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    _, headers = await _new_account(client)
    resp = await _chat(client, headers, stream=True)
    assert resp.status_code == 200
    # The usage-only chunk was asked for by the gateway, so the client never sees it.
    assert '"usage"' not in resp.text
    assert resp.text.rstrip().endswith("data: [DONE]")

    (row,) = (await client.get("/operator/v1/usage", headers=operator_headers)).json()["rows"]
    assert (row["prompt_tokens"], row["completion_tokens"]) == (20, 10)


@pytest.mark.asyncio
async def test_recent_requests_metadata_only_and_filterable(
    client, redis, chat_provider_mock, operator_headers
):
    a_id, a_headers = await _new_account(client)
    _, b_headers = await _new_account(client)
    await _chat(client, a_headers, content="SECRET-PROMPT-TEXT")
    await _chat(client, b_headers)

    data = (await client.get("/operator/v1/requests", headers=operator_headers)).json()["data"]
    assert len(data) == 2
    assert data[0]["outcome"] == "success"
    assert data[0]["prompt_tokens"] == 5 and data[0]["completion_tokens"] == 2
    assert "SECRET-PROMPT-TEXT" not in json.dumps(data)
    assert "messages" not in data[0]

    only_a = (
        await client.get(f"/operator/v1/requests?account_id={a_id}", headers=operator_headers)
    ).json()["data"]
    assert [r["account_id"] for r in only_a] == [a_id]


@pytest.mark.asyncio
async def test_unserved_requests_do_not_grow_the_usage_counters(
    client, redis, chat_provider_mock, operator_headers
):
    _, headers = await _new_account(client)
    # Unknown model: pool exhausted. Must not create a day-hash field for the
    # client-chosen model string.
    resp = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "made-up-model", "messages": [{"role": "user", "content": "x"}]},
    )
    await drain()
    assert resp.status_code == 503

    assert (await client.get("/operator/v1/usage", headers=operator_headers)).json()["rows"] == []
    recent = (await client.get("/operator/v1/requests", headers=operator_headers)).json()["data"]
    assert recent[0]["outcome"] == "exhausted_503"


@pytest.mark.asyncio
async def test_recent_list_is_capped(redis, monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "usage_recent_max", 10)
    store = UsageStore(redis)
    for i in range(25):
        await store.record_request(
            request_id=f"r{i}", account_id="a", session_id=None, model="m",
            status_code=200, outcome="success", latency_ms=1, stream=False,
            attempts=1, prompt_tokens=1, completion_tokens=1,
        )
    recent = await store.recent(100)
    assert len(recent) == 10
    assert recent[0]["request_id"] == "r24"  # newest first


@pytest.mark.asyncio
async def test_model_names_containing_pipe_are_parsed(redis):
    store = UsageStore(redis)
    await store.record_request(
        request_id="r", account_id="acct", session_id=None, model="a|b|c",
        status_code=200, outcome="success", latency_ms=1, stream=False,
        attempts=1, prompt_tokens=3, completion_tokens=4,
    )
    (row,) = (await store.usage(1))["rows"]
    assert (row["account_id"], row["model"], row["total_tokens"]) == ("acct", "a|b|c", 7)


@pytest.mark.asyncio
async def test_operator_session_list_has_tokens_and_no_content(
    client, redis, chat_provider_mock, operator_headers
):
    account_id, headers = await _new_account(client)
    resp = await _chat(client, headers, content="SECRET-PROMPT-TEXT")
    sid = resp.headers["x-session-id"]
    await _chat(client, headers, content="second", session=sid)

    out = await client.get(
        f"/operator/v1/accounts/{account_id}/sessions", headers=operator_headers
    )
    assert out.status_code == 200
    (session,) = out.json()["data"]
    assert session["id"] == sid
    assert session["turn_count"] == 2
    assert (session["prompt_tokens"], session["completion_tokens"]) == (10, 4)
    assert session["last_model"] == "gpt-4o-mini"
    assert "SECRET-PROMPT-TEXT" not in out.text

    missing = await client.get("/operator/v1/accounts/nope/sessions", headers=operator_headers)
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_usage_endpoints_require_operator_key(client, redis):
    _, user_headers = await _new_account(client)
    for path in ("/operator/v1/usage", "/operator/v1/requests"):
        assert (await client.get(path)).status_code == 401
        # A user key is the wrong realm.
        assert (await client.get(path, headers=user_headers)).status_code in (401, 403)


@pytest.mark.asyncio
async def test_failed_tries_are_recorded_with_status_and_class(
    client, redis, chat_provider_mock, operator_headers, monkeypatch
):
    from app.pool.models import KeyCreate, Provider
    from app.pool.registry import PoolRegistry
    from app.provider.adapter import ProviderAttemptError
    from tests.conftest import _FakeBlockingResponse

    # A second key for the same model so failover has somewhere to go.
    await PoolRegistry(redis).create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="second-provider-key")
    )
    calls = []

    async def fake_attempt(reg, key_id, model, messages, **params):
        calls.append(key_id)
        if len(calls) == 1:
            err = ProviderAttemptError(429, "rate_limit", "SECRET provider text", Provider.OPENAI)
            return AttemptResult(ok=False, error=err)
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)
    _, headers = await _new_account(client)
    assert (await _chat(client, headers)).status_code == 200

    (record,) = (await client.get("/operator/v1/requests", headers=operator_headers)).json()["data"]
    assert record["attempts"] == 2
    assert record["failures"] == [{"status_code": 429, "classification": "transient"}]
    assert "SECRET" not in json.dumps(record)


@pytest.mark.asyncio
async def test_stream_break_records_the_cause_without_leaking_it(
    client, redis, chat_provider_mock, operator_headers, monkeypatch, caplog
):
    class _Boom(Exception):
        status_code = 502

    async def broken():
        from tests.conftest import _FakeChunk

        yield _FakeChunk(content="partial")
        raise _Boom("upstream said sk-live-abcdef123456 is bad")

    async def fake_attempt(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=broken())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)
    _, headers = await _new_account(client)

    with caplog.at_level("WARNING", logger="palimpsest.chat"):
        resp = await _chat(client, headers, stream=True)

    assert "provider_stream_interrupted" in resp.text  # the client's generic error
    assert "sk-live" not in resp.text
    (record,) = (await client.get("/operator/v1/requests", headers=operator_headers)).json()["data"]
    assert record["outcome"] == "stream_interrupted"
    assert record["failures"] == [{"status_code": None, "classification": "stream:_Boom 502"}]
    assert "stream:_Boom 502" in caplog.text
    assert "sk-live-abcdef123456" not in caplog.text  # redacted
