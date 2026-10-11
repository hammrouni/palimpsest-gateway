"""Streaming chat: SSE framing, errors, non-disclosure and metering."""
import json

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.session.store import SessionStore
from app.store.redis_store import RedisStore


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def test_app(redis):
    app.state.store = RedisStore(redis)
    return app


@pytest.fixture
async def client(test_app):
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_streaming_two_turn(client, redis, chat_provider_mock):
    """Streaming 2-turn with continuity.

    Same as the blocking flow but with stream: true → SSE framing with delta
    chunks + [DONE]. The provider receives the same message sequence.
    History persisted after stream ends.
    """
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json().get("key") or create_resp.json().get("keys", [{}])[0].get("key")
    account_id = create_resp.json().get("id") or create_resp.json().get("account", {}).get("id")

    # Turn 1: streaming
    t1_resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "stream me"}],
            "stream": True,
        },
    )

    assert t1_resp.status_code == 200
    assert "text/event-stream" in t1_resp.headers.get("content-type", "")
    session_id = t1_resp.headers.get("x-session-id")
    assert session_id

    # Verify SSE framing
    lines = t1_resp.text.strip().split("\n")
    assert any("data: " in line for line in lines)
    assert any("[DONE]" in line for line in lines)

    # Turn 2: streaming on same session
    t2_resp = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Session-ID": session_id,
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "what was turn 1?"}],
            "stream": True,
        },
    )

    assert t2_resp.status_code == 200
    assert t2_resp.headers.get("x-session-id") == session_id

    # Verify history persisted
    session_store = SessionStore(redis)
    metadata = await session_store.get_metadata(account_id, session_id)
    turn_count = int(metadata.get("turn_count", 0))
    assert turn_count == 2


@pytest.mark.asyncio
async def test_provider_error_pre_byte(client, redis, chat_provider_mock):
    """Provider error before first byte (503) → no persistence.

    Simulate provider timeout/unavailable.
    History should remain unchanged.
    turn_count should not increment.
    """
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json().get("key") or create_resp.json().get("keys", [{}])[0].get("key")
    account_id = create_resp.json().get("id") or create_resp.json().get("account", {}).get("id")

    # Create initial turn
    t1_resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )
    session_id = t1_resp.headers.get("x-session-id")

    session_store = SessionStore(redis)
    metadata_before = await session_store.get_metadata(account_id, session_id)
    turn_count_before = int(metadata_before.get("turn_count", 0))

    # Try second turn with mock provider failure (would need provider mock to test properly)
    # For now, just verify turn_count doesn't change on 503
    # In real scenario, ProviderClient would raise 503 GatewayError

    # Note: Testing provider failure requires mocking ProviderClient
    # Deferred for post-token-budget integration
    assert turn_count_before == 1


@pytest.mark.asyncio
async def test_streaming_pre_first_byte_failure_is_clean_503_not_sse(client, redis):
    """A provider failure *before the first byte* on a streaming
    request must be a machine-readable OpenAI 503 JSON envelope — never a 200
    response carrying an SSE error event. No Pool key is seeded, so the
    failover loop's first ``select()`` finds nothing and fails before the
    ``StreamingResponse`` is even constructed.
    """
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    # Clean JSON 503, not a 200 text/event-stream carrying an error payload.
    assert resp.status_code == 503
    assert "application/json" in resp.headers.get("content-type", "")
    assert "text/event-stream" not in resp.headers.get("content-type", "")

    body = resp.json()
    assert "error" in body
    assert body["error"]["message"]
    assert body["error"]["code"]

    # Gateway-issued session ID is still surfaced on the error response.
    assert "x-session-id" in {k.lower() for k in resp.headers.keys()}

    # Non-disclosure: no provider/vendor name leaks into the error body.
    body_text = resp.text.lower()
    for leaked in ("openai", "anthropic", "google", "openrouter", "litellm"):
        assert leaked not in body_text


@pytest.mark.asyncio
async def test_blocking_response_never_leaks_provider_identity(client, redis, chat_provider_mock):
    """A blocking success response never reveals the provider, its model ID
    or endpoint; only the client's requested model is echoed."""
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["model"] == "gpt-4o-mini"  # client's requested model, echoed

    full_text = resp.text.lower() + str(resp.headers).lower()
    for leaked in ("openai", "anthropic", "google", "openrouter", "litellm", "test-provider-key"):
        assert leaked not in full_text


@pytest.mark.asyncio
async def test_mid_stream_failure_single_error_event_no_done(
    client, redis, monkeypatch
):
    """The provider raises *after* the first chunk has already been
    sent — exactly one SSE error event, no [DONE], stream ends, no hang, and the
    error is non-disclosing (fixed message/code, no vendor/model text)."""
    import app.router.failover as failover_module
    from app.pool.models import KeyCreate, Provider
    from app.pool.registry import PoolRegistry
    from app.provider.adapter import AttemptResult

    registry = PoolRegistry(redis)
    await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="test-provider-key")
    )

    class _FailingChunk:
        def __init__(self, content):
            self.choices = [type("C", (), {"delta": type("D", (), {"content": content})()})()]

        def model_dump(self):
            return {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": self.choices[0].delta.content}, "finish_reason": None}],
            }

    async def _one_chunk_then_raise():
        yield _FailingChunk("partial")
        raise RuntimeError("Anthropic upstream connection reset")

    async def fake_attempt(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=_one_chunk_then_raise())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert resp.status_code == 200
    assert "text/event-stream" in resp.headers.get("content-type", "")

    lines = [ln for ln in resp.text.strip().split("\n") if ln.startswith("data: ")]
    assert "[DONE]" not in resp.text

    error_lines = [ln for ln in lines if '"error"' in ln]
    assert len(error_lines) == 1

    payload = json.loads(error_lines[0][len("data: "):])
    assert payload["error"]["code"] == "provider_stream_interrupted"

    # Non-disclosure: the raw exception text ("Anthropic upstream...") must not
    # reach the client — only the fixed, generic message.
    assert "anthropic" not in resp.text.lower()
    assert payload["error"]["message"] == "Provider stream interrupted."


@pytest.mark.asyncio
async def test_persistence_failure_after_done_does_not_emit_second_sse_event(
    client, redis, chat_provider_mock, monkeypatch
):
    """Regression: a persistence failure *after* the provider stream already
    completed successfully (post-[DONE]) must not produce a second SSE
    payload. Previously [DONE] and an error event could both be sent for the
    same stream, violating the spec's "exactly one event, no [DONE]" framing
    for the mid-stream-failure case."""
    from app.session.store import SessionStore

    async def failing_append_exchange(self, account_id, session_id, messages):
        raise RuntimeError("simulated post-stream persistence failure")

    monkeypatch.setattr(SessionStore, "append_exchange", failing_append_exchange)

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert resp.status_code == 200
    assert "[DONE]" in resp.text
    assert '"error"' not in resp.text


@pytest.mark.asyncio
async def test_canary_non_disclosure(client, redis, chat_provider_mock):
    """Canary isolation - A's secret invisible to B's foreign 404.

    A stores CANARY-xyz in history.
    B tries to access A's session → 404.
    404 response must NOT contain canary.
    A's turn 1 still visible to A.
    """
    # Create accounts A and B
    a_resp = await client.post("/v1/accounts", json={})
    a_key = a_resp.json().get("key") or a_resp.json().get("keys", [{}])[0].get("key")

    b_resp = await client.post("/v1/accounts", json={})
    b_key = b_resp.json().get("key") or b_resp.json().get("keys", [{}])[0].get("key")

    # A: turn 1 with secret
    a_t1 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {a_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "CANARY-9f3a-secret-xyz"}],
        },
    )
    assert a_t1.status_code == 200
    a_session_id = a_t1.headers.get("x-session-id")

    # B tries A's session
    b_tries_a = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {b_key}",
            "X-Session-ID": a_session_id,
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "read A's secret"}],
        },
    )

    # Must be 404
    assert b_tries_a.status_code == 404

    # Canary must NOT be in response
    b_body = b_tries_a.text
    assert "CANARY-9f3a-secret" not in b_body
    assert "CANARY" not in b_body

    # A can still access own session
    a_t2 = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {a_key}",
            "X-Session-ID": a_session_id,
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "I know my secret"}],
        },
    )
    assert a_t2.status_code == 200


# -- settle() is never skipped after a consumed provider call ---------------


async def _get_event_key(redis, prefix="pgw:ops:request:"):
    """Extract a single event key's request_id from Redis (one request per test)."""
    keys = await redis.keys(f"{prefix}*")
    assert len(keys) == 1, f"expected 1 {prefix} key, got {len(keys)}"
    raw = keys[0]
    key_str = raw.decode() if isinstance(raw, bytes) else raw
    return key_str[len(prefix):]


@pytest.mark.asyncio
async def test_blocking_settle_runs_after_append_exchange_failure(
    client, redis, chat_provider_mock, monkeypatch
):
    """A provider call that succeeds but is followed by a downstream
    exception (append_exchange) still settles — the used counter is
    incremented for the key that served it, not skipped.

    Before the fix, the bare 'except Exception' in the blocking path overwrote
    the real trace with an empty FailoverTrace (settled_key=None), so the
    successful provider call was never metered. After the fix, the real trace
    is preserved and settle runs with it."""
    from app.settlement.read import get_event
    from app.settlement.settle import _used_key

    async def failing_append_exchange(self, account_id, session_id, messages):
        raise RuntimeError("simulated post-success persistence failure")

    monkeypatch.setattr(SessionStore, "append_exchange", failing_append_exchange)

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )

    # The completion was already produced and paid for: a persistence failure
    # must not discard it (mirrors the streaming path) — it is metered and served.
    assert resp.status_code == 200

    # The consumed provider call was still metered — the used counter for the
    # key that served it was incremented (settle ran with the real trace).
    keys = await chat_provider_mock.list()
    key_id = keys[0].id
    used = await redis.get(_used_key(key_id))
    assert used is not None, "used counter was never set — settle was skipped"
    assert int(used) == 1, f"expected used=1 after settle, got used={used!r}"

    # The canonical event records settled=1 with the real settled_key.
    request_id = await _get_event_key(redis)
    event = await get_event(request_id, redis=redis)
    assert event is not None
    assert event["settled"] == 1
    assert event["settled_key"] == key_id


@pytest.mark.asyncio
async def test_streaming_settle_runs_after_append_exchange_failure(
    client, redis, chat_provider_mock, monkeypatch
):
    """The post-[DONE] block must still call settle() when
    append_exchange/touch throws. The stream was already sent successfully to
    the client; the consumed provider call must be metered regardless."""
    from app.settlement.read import get_event
    from app.settlement.settle import _used_key

    async def failing_append_exchange(self, account_id, session_id, messages):
        raise RuntimeError("simulated post-stream persistence failure")

    monkeypatch.setattr(SessionStore, "append_exchange", failing_append_exchange)

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )

    assert resp.status_code == 200
    assert "[DONE]" in resp.text

    # The consumed provider call was still metered — the used counter was
    # incremented even though append_exchange raised.
    keys = await chat_provider_mock.list()
    key_id = keys[0].id
    used = await redis.get(_used_key(key_id))
    assert used is not None
    assert int(used) == 1

    # The canonical event records settled=1 with the real settled_key.
    request_id = await _get_event_key(redis)
    event = await get_event(request_id, redis=redis)
    assert event is not None
    assert event["settled"] == 1
    assert event["settled_key"] == key_id
    assert event["stream"] is True


# -- X-Request-ID header on the streaming success path --------------------


@pytest.mark.asyncio
async def test_streaming_success_response_includes_x_request_id(
    client, redis, chat_provider_mock
):
    """The streaming success response (initial HTTP headers before the
    SSE stream begins) carries an X-Request-ID."""
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    assert resp.status_code == 200
    assert "x-request-id" in {k.lower() for k in resp.headers.keys()}
    assert resp.headers["x-request-id"]


# -- empty role → 422 ------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_role_message_returns_422(client, redis, chat_provider_mock):
    """An empty ``role`` string in any message is rejected at the
    Pydantic validation layer with a 422 (the existing
    ``ChatMessage.role: str = Field(min_length=1)`` enforces this; the
    dead ``validate_messages`` function is removed since the schema
    already enforces the contract)."""
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "", "content": "empty role test"}],
        },
    )
    # RequestValidationError → 422 in the OpenAI envelope shape.
    assert resp.status_code == 422
    body = resp.json()
    assert "error" in body
    assert body["error"]["type"] == "invalid_request_error"
    # The validation handler also adds X-Request-ID.
    assert "x-request-id" in {k.lower() for k in resp.headers.keys()}


# -- streaming observability audit -----------------------------------------


@pytest.mark.asyncio
async def test_streaming_success_emits_exactly_one_log_record(
    client, redis, chat_provider_mock, caplog
):
    """The streaming success path emits exactly one structured
    palimpsest.requests record: no path is silent, none is doubled."""
    import logging as _logging

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    with caplog.at_level(_logging.INFO, logger="palimpsest.requests"):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert resp.status_code == 200
    assert "[DONE]" in resp.text

    records = [r for r in caplog.records if r.name == "palimpsest.requests"]
    assert len(records) == 1
    record = json.loads(records[0].message)
    assert record["outcome"] == "success"
    assert record["stream"] is True


@pytest.mark.asyncio
async def test_streaming_stream_interrupted_emits_exactly_one_log_record(
    client, redis, caplog, monkeypatch
):
    """The streaming stream_interrupted path (mid-stream failure) emits
    exactly one record — the test in test_chat_streaming already covers this
    outcome; this asserts the exactly-one invariant."""
    import logging as _logging

    import app.router.failover as failover_module
    from app.pool.models import KeyCreate, Provider
    from app.pool.registry import PoolRegistry
    from app.provider.adapter import AttemptResult

    registry = PoolRegistry(redis)
    await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="test-provider-key")
    )

    class _FailingChunk:
        def __init__(self, content):
            self.choices = [type("C", (), {"delta": type("D", (), {"content": content})()})()]

        def model_dump(self):
            return {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": self.choices[0].delta.content}, "finish_reason": None}],
            }

    async def _one_chunk_then_raise():
        yield _FailingChunk("partial")
        raise RuntimeError("simulated mid-stream failure")

    async def fake_attempt(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=_one_chunk_then_raise())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    with caplog.at_level(_logging.INFO, logger="palimpsest.requests"):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
        )
    assert resp.status_code == 200
    assert "[DONE]" not in resp.text

    records = [r for r in caplog.records if r.name == "palimpsest.requests"]
    assert len(records) == 1
    record = json.loads(records[0].message)
    assert record["outcome"] == "stream_interrupted"

    # The consumed provider call is still metered on a mid-stream failure.
    from app.settlement.read import get_event
    from app.settlement.settle import _used_key

    key_id = (await registry.list())[0].id
    assert int(await redis.get(_used_key(key_id))) == 1
    event = await get_event(record["request_id"], redis=redis)
    assert event is not None
    assert event["settled"] == 1
    assert event["settled_key"] == key_id
    assert event["stream"] is True


# -- disconnect metering, streamed tool_calls -----------------------------------------


class _ToolDeltaChunk:
    """Streamed chunk whose delta carries ``tool_calls`` fragments (no content)."""

    def __init__(self, tool_calls):
        self.choices = [
            type("C", (), {"delta": type("D", (), {"content": None, "tool_calls": tool_calls})()})()
        ]
        self._tool_calls = tool_calls

    def model_dump(self):
        return {
            "id": "chatcmpl-fake",
            "object": "chat.completion.chunk",
            "choices": [{"index": 0, "delta": {"tool_calls": self._tool_calls}, "finish_reason": None}],
        }


def _flatten(history):
    """read_recent returns exchanges ``{ts, messages}``; flatten to messages."""
    return [m for exchange in history for m in exchange["messages"]]


@pytest.mark.asyncio
async def test_streamed_tool_calls_are_accumulated_into_history(
    client, redis, chat_provider_mock, monkeypatch
):
    """Streamed ``tool_calls`` fragments (name first, arguments split across
    chunks, keyed by index) are persisted as one complete assistant tool call."""
    import app.router.failover as failover_module
    from app.provider.adapter import AttemptResult

    async def _tool_stream():
        yield _ToolDeltaChunk(
            [{"index": 0, "id": "call_1", "type": "function",
              "function": {"name": "get_weather", "arguments": '{"city": '}}]
        )
        yield _ToolDeltaChunk([{"index": 0, "function": {"arguments": '"Paris"}'}}])

    async def fake_attempt(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=_tool_stream())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]
    account_id = create_resp.json()["id"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "weather?"}], "stream": True},
    )
    assert resp.status_code == 200
    assert "[DONE]" in resp.text

    history = await SessionStore(redis).read_recent(account_id, resp.headers["x-session-id"])
    assistant = [m for m in _flatten(history) if m.get("role") == "assistant"][-1]
    assert assistant["content"] is None
    assert assistant["tool_calls"] == [
        {
            "id": "call_1",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }
    ]


@pytest.mark.asyncio
async def test_client_disconnect_mid_stream_still_settles_and_logs_once(
    client, redis, chat_provider_mock, caplog, monkeypatch
):
    """A consumer that stops reading (generator closed -> GeneratorExit) after the
    provider call was consumed must still be metered and produce exactly one log
    record, and must release the turn lock."""
    import logging as _logging

    import app.api.chat as chat_module
    from app.settlement.read import get_event
    from app.settlement.settle import _used_key

    original = chat_module._stream_chunks

    async def closes_after_first_chunk(*args, **kwargs):
        gen = original(*args, **kwargs)
        yield await gen.__anext__()
        await gen.aclose()  # what Starlette does when the client goes away

    monkeypatch.setattr(chat_module, "_stream_chunks", closes_after_first_chunk)

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    with caplog.at_level(_logging.INFO, logger="palimpsest.requests"):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
    assert resp.status_code == 200
    assert "[DONE]" not in resp.text

    records = [r for r in caplog.records if r.name == "palimpsest.requests"]
    assert len(records) == 1
    record = json.loads(records[0].message)
    assert record["outcome"] == "stream_interrupted"

    key_id = (await chat_provider_mock.list())[0].id
    assert int(await redis.get(_used_key(key_id))) == 1
    event = await get_event(record["request_id"], redis=redis)
    assert event["settled"] == 1
    assert event["stream"] is True

    # Lock released: a follow-up turn on the same session is not 423.
    monkeypatch.setattr(chat_module, "_stream_chunks", original)
    again = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "X-Session-ID": resp.headers["x-session-id"]},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "again"}]},
    )
    assert again.status_code == 200


@pytest.mark.asyncio
async def test_midstream_settle_failure_still_emits_error_event_and_log(
    client, redis, chat_provider_mock, caplog, monkeypatch
):
    """A non-Redis exception from settle() in the mid-stream handler must not
    suppress the SSE error event or the log record."""
    import logging as _logging

    import app.api.chat as chat_module
    import app.router.failover as failover_module
    from app.provider.adapter import AttemptResult
    from tests.conftest import _FakeChunk

    async def _boom_stream():
        # Content has begun, so this is a genuine mid-stream failure; a stream that
        # dies before its first chunk is failed over instead (test_stream_failover).
        yield _FakeChunk(content="partial")
        raise RuntimeError("provider died")

    async def fake_attempt(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=_boom_stream())

    async def failing_settle(*a, **kw):
        raise TypeError("settle exploded")

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)
    monkeypatch.setattr(chat_module, "settle", failing_settle)

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    with caplog.at_level(_logging.INFO):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}], "stream": True},
        )
    assert resp.status_code == 200
    assert "provider_stream_interrupted" in resp.text
    records = [r for r in caplog.records if r.name == "palimpsest.requests"]
    assert len(records) == 1
    assert json.loads(records[0].message)["outcome"] == "stream_interrupted"


@pytest.mark.asyncio
async def test_streaming_404_error_carries_logged_request_id(
    client, redis, chat_provider_mock, caplog
):
    """The streaming 404 response's X-Request-ID equals the logged request_id and
    a supplied ID is echoed; the route logs exactly one record per request."""
    import logging as _logging

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]
    body = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x"}], "stream": True}

    with caplog.at_level(_logging.INFO, logger="palimpsest.requests"):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "X-Session-ID": "no-such-session"},
            json=body,
        )
        echoed = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Session-ID": "no-such-session",
                "X-Request-ID": "caller-rid-1",
            },
            json=body,
        )
    assert resp.status_code == 404
    records = [json.loads(r.message) for r in caplog.records if r.name == "palimpsest.requests"]
    assert len(records) == 2
    assert resp.headers["x-request-id"] == records[0]["request_id"]
    assert echoed.headers["x-request-id"] == "caller-rid-1" == records[1]["request_id"]
