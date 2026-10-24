"""Request logging through the real chat route: one record per outcome path."""
import json
import logging

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.config import Settings
from app.main import app
from app.session.locks import SessionLock
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
        transport=ASGITransport(app=test_app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture
def fast_lock_settings(monkeypatch):
    """Short lock wait/poll so the 423 test doesn't burn the real 2s default."""
    import app.api.chat as chat_module

    fast = Settings(session_lock_wait_ms=200, session_lock_poll_ms=20)
    monkeypatch.setattr(chat_module, "get_settings", lambda: fast)
    return fast


def _last_record(caplog) -> dict:
    records = [r for r in caplog.records if r.name == "palimpsest.requests"]
    assert records, "no palimpsest.requests log record was emitted"
    return json.loads(records[-1].message)


@pytest.mark.asyncio
async def test_blocking_success_logs_one_record_with_chain_and_n(
    client, redis, chat_provider_mock, caplog
):
    """The success record carries account/session/outcome/status/latency
    plus the chain/attempts/n fields."""
    create_resp = await client.post("/v1/accounts", json={})
    account_data = create_resp.json()
    api_key = account_data["keys"][0]["key"]

    with caplog.at_level(logging.INFO):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200

    record = _last_record(caplog)
    assert record["account_id"] == account_data["id"]
    assert record["session_id"] == resp.headers["x-session-id"]
    assert record["outcome"] == "success"
    assert record["status_code"] == 200
    assert record["latency_ms"] >= 0
    assert record["chain"] == ["test-provider-key"] or len(record["chain"]) == 1
    assert record["attempts"] == len(record["chain"])
    assert record["n"] == 4  # default VERBATIM_TURNS_N
    assert "messages" not in record  # REQUEST_LOG_PROMPTS default false


@pytest.mark.asyncio
async def test_streaming_success_logs_exactly_one_record(
    client, redis, chat_provider_mock, caplog
):
    """The streaming path logs one record too."""
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    with caplog.at_level(logging.INFO):
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

    records = [r for r in caplog.records if r.name == "palimpsest.requests"]
    assert len(records) == 1  # exactly one record for this request
    record = json.loads(records[0].message)
    assert record["outcome"] == "success"
    assert record["stream"] is True


@pytest.mark.asyncio
async def test_foreign_session_404_logs_rejected_404(client, redis, chat_provider_mock, caplog):
    """The 404-foreign path must also emit a record."""
    a_resp = await client.post("/v1/accounts", json={})
    a_key = a_resp.json()["keys"][0]["key"]
    b_resp = await client.post("/v1/accounts", json={})
    b_key = b_resp.json()["keys"][0]["key"]

    a_turn = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {a_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "secret"}]},
    )
    session_id = a_turn.headers["x-session-id"]

    with caplog.at_level(logging.INFO):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {b_key}", "X-Session-ID": session_id},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "snoop"}]},
        )
    assert resp.status_code == 404

    record = _last_record(caplog)
    assert record["outcome"] == "rejected_404"
    assert record["status_code"] == 404
    # Non-disclosure: A's content never appears in B's rejection log record.
    assert "secret" not in json.dumps(record)


@pytest.mark.asyncio
async def test_busy_423_logs_busy_423(client, redis, chat_provider_mock, fast_lock_settings, caplog):
    """The 423 busy path must emit a record too."""
    create_resp = await client.post("/v1/accounts", json={})
    account_data = create_resp.json()
    account_id = account_data["id"]
    api_key = account_data["keys"][0]["key"]

    setup_resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    session_id = setup_resp.headers["x-session-id"]

    store = SessionStore(redis)
    lock = SessionLock(redis)
    lock_key = store._lock_key(account_id, session_id)
    held, holder_token = await lock.acquire(lock_key)
    assert held

    with caplog.at_level(logging.INFO):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "X-Session-ID": session_id},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "waiter"}]},
        )
    assert resp.status_code == 423

    record = _last_record(caplog)
    assert record["outcome"] == "busy_423"
    assert record["status_code"] == 423
    assert record["chain"] == []  # never reached key selection

    await lock.release(lock_key, holder_token)


@pytest.mark.asyncio
async def test_pool_exhausted_logs_exhausted_429(client, redis, caplog):
    """No routable pool key -> PoolExhaustedError -> 429, still one record."""
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    with caplog.at_level(logging.INFO):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 429

    record = _last_record(caplog)
    assert record["outcome"] == "exhausted_429"
    assert record["status_code"] == 429


@pytest.mark.asyncio
async def test_prompts_opt_in_logs_current_turn_only(
    client, redis, chat_provider_mock, caplog, monkeypatch
):
    """REQUEST_LOG_PROMPTS=true logs only the current turn's messages —
    never another tenant's stored history."""
    import app.logging.request_logger as logger_module

    enabled = Settings(request_log_prompts=True)
    monkeypatch.setattr(logger_module, "get_settings", lambda: enabled)

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    with caplog.at_level(logging.INFO):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "CANARY-current-turn"}],
            },
        )
    assert resp.status_code == 200

    record = _last_record(caplog)
    assert "messages" in record
    contents = [m["content"] for m in record["messages"]]
    assert contents == ["CANARY-current-turn"]


@pytest.mark.asyncio
async def test_raising_log_sink_does_not_change_client_outcome(
    client, redis, chat_provider_mock, monkeypatch
):
    """A logging-sink fault must never change the client's
    outcome. Regression: before this was fixed, `log_request` had no failure
    containment at all — a raising sink turned a normal 200 into an unhandled
    exception (no HTTP response, not even a clean 500)."""
    import app.logging.request_logger as logger_module

    def _raise(*args, **kwargs):
        raise RuntimeError("simulated logging sink outage")

    monkeypatch.setattr(logger_module.logger, "info", _raise)

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    assert resp.json()["choices"][0]["message"]["role"] == "assistant"


@pytest.mark.asyncio
async def test_settle_failure_after_success_logs_exactly_one_record_blocking(
    client, redis, chat_provider_mock, caplog, monkeypatch
):
    """settle() failing after a successful provider response yields one
    accurate failure record, never a success record plus a failure one."""
    import app.api.chat as chat_module

    async def failing_settle(*args, **kwargs):
        raise RuntimeError("simulated settlement outage")

    monkeypatch.setattr(chat_module, "settle", failing_settle)

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    with caplog.at_level(logging.INFO):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )

    records = [r for r in caplog.records if r.name == "palimpsest.requests"]
    assert len(records) == 1, f"expected exactly one record, got {len(records)}"
    record = json.loads(records[0].message)
    # The one record must match what the client actually received, not a
    # phantom "success" for a request that ultimately failed.
    assert record["status_code"] == resp.status_code
    assert resp.status_code != 200


@pytest.mark.asyncio
async def test_settle_failure_after_streaming_done_still_logs_one_record(
    client, redis, chat_provider_mock, caplog, monkeypatch
):
    """A settlement failure after a clean [DONE] still writes one record."""
    import app.api.chat as chat_module

    async def failing_settle(*args, **kwargs):
        raise RuntimeError("simulated settlement outage")

    monkeypatch.setattr(chat_module, "settle", failing_settle)

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    with caplog.at_level(logging.INFO):
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
    assert record["outcome"] == "success_persist_failed"


# -- every chat path emits exactly one log record ---------


@pytest.mark.asyncio
async def test_blocking_success_emits_exactly_one_log_record(
    client, redis, chat_provider_mock, caplog
):
    """The blocking success path emits exactly one structured
    palimpsest.requests record (not zero, not two)."""
    import logging as _logging

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    with caplog.at_level(_logging.INFO, logger="palimpsest.requests"):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    assert resp.status_code == 200

    records = [r for r in caplog.records if r.name == "palimpsest.requests"]
    assert len(records) == 1
    record = json.loads(records[0].message)
    assert record["outcome"] == "success"


@pytest.mark.asyncio
async def test_blocking_rejected_404_emits_exactly_one_log_record(
    client, redis, chat_provider_mock, caplog
):
    """The blocking 404 (foreign session) path emits exactly one record."""
    import logging as _logging

    a = await client.post("/v1/accounts", json={})
    a_key = a.json()["keys"][0]["key"]
    a_t1 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {a_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    a_session = a_t1.headers["x-session-id"]

    b = await client.post("/v1/accounts", json={})
    b_key = b.json()["keys"][0]["key"]

    with caplog.at_level(_logging.INFO, logger="palimpsest.requests"):
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {b_key}", "X-Session-ID": a_session},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x"}]},
        )
    assert resp.status_code == 404

    records = [r for r in caplog.records if r.name == "palimpsest.requests"]
    assert len(records) == 1
    record = json.loads(records[0].message)
    assert record["outcome"] == "rejected_404"


# -- X-Request-ID header on all chat response paths ----------------------


@pytest.mark.asyncio
async def test_blocking_success_response_includes_x_request_id(
    client, redis, chat_provider_mock
):
    """The blocking success response carries an X-Request-ID header
    (generated by the gateway when the client doesn't supply one)."""
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert "x-request-id" in {k.lower() for k in resp.headers.keys()}
    rid = resp.headers["x-request-id"]
    assert rid and len(rid) > 0


@pytest.mark.asyncio
async def test_x_request_id_header_is_echoed_when_supplied(
    client, redis, chat_provider_mock
):
    """A client-supplied X-Request-ID is echoed back unchanged."""
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    supplied_rid = "client-supplied-request-id-12345"
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "X-Request-ID": supplied_rid},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] == supplied_rid


@pytest.mark.asyncio
async def test_blocking_rejected_404_response_includes_x_request_id(
    client, redis, chat_provider_mock
):
    """The blocking 404 (foreign session) response carries X-Request-ID."""
    a = await client.post("/v1/accounts", json={})
    a_key = a.json()["keys"][0]["key"]
    a_t1 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {a_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    a_session = a_t1.headers["x-session-id"]

    b = await client.post("/v1/accounts", json={})
    b_key = b.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {b_key}", "X-Session-ID": a_session},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "x"}]},
    )
    assert resp.status_code == 404
    assert "x-request-id" in {k.lower() for k in resp.headers.keys()}


@pytest.mark.asyncio
async def test_blocking_busy_423_response_includes_x_request_id(
    client, redis, chat_provider_mock
):
    """The blocking 423 (session busy) response carries X-Request-ID."""
    from app.session.locks import SessionLock

    create_resp = await client.post("/v1/accounts", json={})
    api_data = create_resp.json()
    api_key = api_data["keys"][0]["key"]
    account_id = api_data["id"]

    # Set up a session and lock it externally to force a busy response.
    setup = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    session_id = setup.headers["x-session-id"]

    from app.session.store import SessionStore

    store = SessionStore(redis)
    lock = SessionLock(redis)
    lock_key = store._lock_key(account_id, session_id)
    held, holder_token = await lock.acquire(lock_key)
    assert held

    # Fast lock settings so the busy response arrives quickly.
    import app.api.chat as chat_module
    from app.config import Settings

    fast = Settings(session_lock_wait_ms=200, session_lock_poll_ms=20)
    original_settings = chat_module.get_settings
    chat_module.get_settings = lambda: fast

    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "X-Session-ID": session_id},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "waiter"}]},
        )
    finally:
        chat_module.get_settings = original_settings
        await lock.release(lock_key, holder_token)

    assert resp.status_code == 423
    assert "x-request-id" in {k.lower() for k in resp.headers.keys()}


# -- tool_calls preservation -------------------------------------------------


class _FakeToolMessage:
    """A minimal assistant message containing tool_calls."""

    def __init__(self):
        self.role = "assistant"
        self.content = None
        self.tool_calls = [
            {
                "id": "call_abc123",
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "arguments": '{"city": "Paris"}',
                },
            }
        ]

    def model_dump(self) -> dict:
        return {
            "role": "assistant",
            "content": None,
            "tool_calls": self.tool_calls,
        }


class _FakeToolBlockingResponse:
    """Litellm ModelResponse stand-in carrying tool_calls."""

    def __init__(self):
        self.id = "chatcmpl-tool-test"
        self.choices = [type("C", (), {"message": _FakeToolMessage()})()]
        self.usage = type("U", (), {"total_tokens": 25, "prompt_tokens": 20, "completion_tokens": 5})()


@pytest.mark.asyncio
async def test_blocking_response_preserves_tool_calls(
    client, redis, chat_provider_mock, monkeypatch
):
    """A provider response containing tool_calls passes through to the
    blocking client response — the tool_calls field is not silently dropped."""
    import app.router.failover as failover_module
    from app.provider.adapter import AttemptResult

    async def fake_attempt(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=_FakeToolBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "what's the weather?"}]},
    )
    assert resp.status_code == 200
    body = resp.json()
    # tool_calls preserved in the response choice's message.
    assert body["choices"][0]["message"]["tool_calls"] == [
        {
            "id": "call_abc123",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"city": "Paris"}'},
        }
    ]


# -- error-path request IDs, one-record invariant, route-level admission,
#    param forwarding, finish_reason, null-key stripping --------------------------


def _records(caplog) -> list[dict]:
    return [json.loads(r.message) for r in caplog.records if r.name == "palimpsest.requests"]


async def _new_account(client) -> tuple[str, str]:
    resp = await client.post("/v1/accounts", json={})
    return resp.json()["keys"][0]["key"], resp.json()["id"]


_BODY = {"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]}


@pytest.mark.asyncio
async def test_exhausted_429_header_equals_logged_request_id_and_echoes_supplied(
    client, redis, caplog
):
    """429 (no routable key): the header is the logged request_id, not a fresh ID,
    and a caller-supplied ID is used for both."""
    api_key, _ = await _new_account(client)
    with caplog.at_level(logging.INFO, logger="palimpsest.requests"):
        plain = await client.post(
            "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=_BODY
        )
        supplied = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "X-Request-ID": "rid-429"},
            json=_BODY,
        )
    assert plain.status_code == supplied.status_code == 429
    recs = _records(caplog)
    assert len(recs) == 2  # exactly one record per request
    assert plain.headers["x-request-id"] == recs[0]["request_id"]
    assert supplied.headers["x-request-id"] == "rid-429" == recs[1]["request_id"]


@pytest.mark.asyncio
async def test_blocking_404_and_422_header_equals_logged_request_id(
    client, redis, chat_provider_mock, caplog
):
    api_key, _ = await _new_account(client)
    with caplog.at_level(logging.INFO, logger="palimpsest.requests"):
        not_found = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "X-Session-ID": "unknown-session"},
            json=_BODY,
        )
        bad_id = await client.post(
            "/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "X-Session-ID": "bad id!",
                "X-Request-ID": "rid-422",
            },
            json=_BODY,
        )
    assert not_found.status_code == 404
    recs = _records(caplog)
    assert len(recs) == 1  # 422 is rejected pre-log; the 404 logs exactly one
    assert not_found.headers["x-request-id"] == recs[0]["request_id"]
    assert bad_id.status_code == 422
    assert bad_id.headers["x-request-id"] == "rid-422"


@pytest.mark.asyncio
async def test_busy_423_emits_exactly_one_record_with_matching_header(
    client, redis, chat_provider_mock, fast_lock_settings, caplog
):
    api_key, account_id = await _new_account(client)
    setup = await client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=_BODY
    )
    session_id = setup.headers["x-session-id"]
    lock_key = SessionStore(redis)._lock_key(account_id, session_id)
    lock = SessionLock(redis)
    held, token = await lock.acquire(lock_key)
    assert held
    try:
        with caplog.at_level(logging.INFO, logger="palimpsest.requests"):
            resp = await client.post(
                "/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "X-Session-ID": session_id},
                json=_BODY,
            )
    finally:
        await lock.release(lock_key, token)
    assert resp.status_code == 423
    recs = _records(caplog)
    assert len(recs) == 1
    assert recs[0]["outcome"] == "busy_423"
    assert resp.headers["x-request-id"] == recs[0]["request_id"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stream", [False, True])
async def test_user_cap_429_route_level(client, redis, chat_provider_mock, caplog, monkeypatch, stream):
    """The per-user cap reaches real traffic: 429 + code, one record, matching
    header, and no provider call / session created for the rejected request."""
    import app.settlement.admission as admission_module

    capped = Settings(user_daily_cap=1)
    monkeypatch.setattr(admission_module, "get_settings", lambda: capped)
    api_key, _ = await _new_account(client)
    headers = {"Authorization": f"Bearer {api_key}"}
    first = await client.post("/v1/chat/completions", headers=headers, json=_BODY)
    assert first.status_code == 200
    calls_before = len(chat_provider_mock.received_calls)

    with caplog.at_level(logging.INFO, logger="palimpsest.requests"):
        resp = await client.post(
            "/v1/chat/completions", headers=headers, json={**_BODY, "stream": stream}
        )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "user_quota_exhausted"
    recs = _records(caplog)
    assert len(recs) == 1
    assert recs[0]["outcome"] == "rejected_user_cap"
    assert resp.headers["x-request-id"] == recs[0]["request_id"]
    assert len(chat_provider_mock.received_calls) == calls_before
    assert "x-session-id" not in resp.headers


@pytest.mark.asyncio
async def test_low_water_429_route_level(client, redis, chat_provider_mock, caplog, monkeypatch):
    import app.settlement.admission as admission_module
    from app.pool.models import KeyCreate, Provider
    from app.pool.registry import PoolRegistry

    # Replace the uncapped seed key's role: one capped key with 5 left <= mark 10.
    await PoolRegistry(redis).create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="capped-key", daily_cap=5)
    )
    throttled = Settings(pool_low_water_mark=10)
    monkeypatch.setattr(admission_module, "get_settings", lambda: throttled)
    api_key, _ = await _new_account(client)

    with caplog.at_level(logging.INFO, logger="palimpsest.requests"):
        resp = await client.post(
            "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=_BODY
        )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "pool_low_water"
    recs = _records(caplog)
    assert len(recs) == 1
    assert recs[0]["outcome"] == "rejected_pool_low_water"
    assert resp.headers["x-request-id"] == recs[0]["request_id"]


@pytest.mark.asyncio
async def test_low_water_gate_ignores_pool_with_no_capped_keys(
    client, redis, chat_provider_mock, monkeypatch
):
    """An all-uncapped pool has no finite capacity to run low on: not throttled."""
    import app.settlement.admission as admission_module

    monkeypatch.setattr(admission_module, "get_settings", lambda: Settings(pool_low_water_mark=10))
    api_key, _ = await _new_account(client)
    resp = await client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=_BODY
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_generation_params_are_forwarded_to_the_provider(client, redis, chat_provider_mock, monkeypatch):
    import app.router.failover as failover_module
    from app.provider.adapter import AttemptResult
    from tests.conftest import _FakeBlockingResponse

    seen: dict = {}

    async def fake_attempt(reg, key_id, model, messages, **params):
        seen.update(params)
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)
    api_key, _ = await _new_account(client)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={**_BODY, "temperature": 0.2, "max_tokens": 50, "stop": ["END"]},
    )
    assert resp.status_code == 200
    assert seen["temperature"] == 0.2
    assert seen["max_tokens"] == 50
    assert seen["stop"] == ["END"]
    assert "seed" not in seen and "top_p" not in seen  # unset params are not forwarded


class _LengthMessage:
    def model_dump(self) -> dict:
        return {
            "role": "assistant",
            "content": "cut o",
            "tool_calls": None,
            "function_call": None,
        }


class _LengthResponse:
    def __init__(self):
        self.id = "chatcmpl-length"
        self.choices = [type("C", (), {"message": _LengthMessage(), "finish_reason": "length"})()]
        self.usage = type("U", (), {"total_tokens": 10, "prompt_tokens": 5, "completion_tokens": 5})()


@pytest.mark.asyncio
async def test_blocking_response_keeps_provider_finish_reason_and_drops_null_keys(
    client, redis, chat_provider_mock, monkeypatch
):
    import app.router.failover as failover_module
    from app.provider.adapter import AttemptResult

    async def fake_attempt(reg, key_id, model, messages, **params):
        return AttemptResult(ok=True, response=_LengthResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)
    api_key, _ = await _new_account(client)
    resp = await client.post(
        "/v1/chat/completions", headers={"Authorization": f"Bearer {api_key}"}, json=_BODY
    )
    assert resp.status_code == 200
    choice = resp.json()["choices"][0]
    assert choice["finish_reason"] == "length"
    assert choice["message"] == {"role": "assistant", "content": "cut o"}


def test_settle_id_ttl_seconds_rejects_non_positive():
    from pydantic import ValidationError

    for bad in (0, -5):
        with pytest.raises(ValidationError):
            Settings(settle_id_ttl_seconds=bad)


# -- client X-Request-ID is validated and never the settle key ----------------------


@pytest.mark.asyncio
async def test_reused_client_request_id_does_not_suppress_second_settlement(
    client, redis, chat_provider_mock, caplog
):
    """Two requests sending the same X-Request-ID are both echoed it, both logged,
    and both metered (distinct settle_id)."""
    from app.settlement.settle import _used_key

    api_key, _ = await _new_account(client)
    headers = {"Authorization": f"Bearer {api_key}", "X-Request-ID": "reused-id"}
    with caplog.at_level(logging.INFO, logger="palimpsest.requests"):
        first = await client.post("/v1/chat/completions", headers=headers, json=_BODY)
        second = await client.post("/v1/chat/completions", headers=headers, json=_BODY)
    assert first.headers["x-request-id"] == second.headers["x-request-id"] == "reused-id"
    recs = _records(caplog)
    assert [r["request_id"] for r in recs] == ["reused-id", "reused-id"]
    assert recs[0]["settle_id"] != recs[1]["settle_id"]
    key_id = (await chat_provider_mock.list())[0].id
    assert int(await redis.get(_used_key(key_id))) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["x" * 129, "has space", "semi;colon", "new\tline"])
async def test_malformed_client_request_id_is_replaced(client, redis, chat_provider_mock, bad):
    api_key, _ = await _new_account(client)
    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "X-Request-ID": bad},
        json=_BODY,
    )
    assert resp.status_code == 200
    assert resp.headers["x-request-id"] != bad
    assert len(resp.headers["x-request-id"]) > 0
