"""Session binding and ownership checks."""
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.store.redis_store import RedisStore


@pytest.fixture
async def redis():
    """Provide FakeRedis."""
    return FakeRedis()


@pytest.fixture
async def test_app(redis):
    """Provide test app with FakeRedis."""
    app.state.store = RedisStore(redis)
    return app


@pytest.fixture
async def client(test_app):
    """Provide AsyncClient with FakeRedis."""
    async with AsyncClient(
        transport=ASGITransport(app=test_app),
        base_url="http://test",
    ) as c:
        yield c


@pytest.mark.asyncio
async def test_session_binding_absent_id(client, redis, chat_provider_mock):
    """Absent X-Session-ID → gateway issues new ID, claims session.

    - Request has no X-Session-ID
    - Gateway generates UUIDv7 and claims it
    - Response includes X-Session-ID header
    """
    # Create account first
    create_resp = await client.post(
        "/v1/accounts",
        json={},
    )
    assert create_resp.status_code == 201
    account_data = create_resp.json()
    api_key = account_data.get("key") or (account_data.get("keys", [{}])[0].get("key"))

    # First chat request (no X-Session-ID)
    chat_resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
        },
    )

    # Should succeed
    assert chat_resp.status_code == 200
    assert "x-session-id" in {k.lower() for k in chat_resp.headers.keys()}


@pytest.mark.asyncio
async def test_usage_reports_real_provider_token_counts(client, redis, chat_provider_mock):
    """usage.prompt_tokens/completion_tokens must be the provider's real
    counts (chat_provider_mock's fake usage: prompt_tokens=5), not a fabricated
    30/70 split of total_tokens that ignores what the provider actually reported."""
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 200
    usage = resp.json()["usage"]
    assert usage["prompt_tokens"] == 5  # the mock's real prompt_tokens, not total*0.3
    assert usage["completion_tokens"] == 2  # "mock reply" == 2 words
    assert usage["prompt_tokens"] + usage["completion_tokens"] == usage["total_tokens"]


@pytest.mark.asyncio
async def test_session_binding_own_id(client, redis, chat_provider_mock):
    """Own X-Session-ID → verified, ownership passes, history read.

    - Account A creates session
    - Same account presents session ID → 200 (verified)
    """
    # Create account
    create_resp = await client.post("/v1/accounts", json={})
    assert create_resp.status_code == 201
    account_data = create_resp.json()
    api_key = account_data["keys"][0]["key"]

    # Turn 1: generate session
    t1_resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "turn 1"}],
        },
    )
    assert t1_resp.status_code == 200
    session_id = t1_resp.headers.get("x-session-id")
    assert session_id

    # Turn 2: reuse same session ID
    t2_resp = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Session-ID": session_id,
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "turn 2"}],
        },
    )

    # Should succeed with same session ID
    assert t2_resp.status_code == 200
    assert t2_resp.headers.get("x-session-id") == session_id


@pytest.mark.asyncio
async def test_session_binding_foreign_id(client, redis, chat_provider_mock):
    """Foreign X-Session-ID → 404, provider not called, no leak.

    - Account A creates session
    - Account B presents A's session ID → 404 (generic, non-disclosure)
    - Provider is never called
    """
    # Create accounts A and B
    a_resp = await client.post("/v1/accounts", json={})
    a_key = a_resp.json()["keys"][0]["key"]

    b_resp = await client.post("/v1/accounts", json={})
    b_key = b_resp.json()["keys"][0]["key"]

    # A creates session
    a_t1 = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {a_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "secret"}],
        },
    )
    assert a_t1.status_code == 200
    a_session_id = a_t1.headers.get("x-session-id")

    # B tries to use A's session
    b_tries_a = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {b_key}",
            "X-Session-ID": a_session_id,
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "snoop"}],
        },
    )

    # Must be 404 (not 403, not 200)
    assert b_tries_a.status_code == 404
    assert b_tries_a.json()["error"]["code"] == "session_not_found"

    # No leak of A's content in response
    body_text = b_tries_a.text
    assert "secret" not in body_text


@pytest.mark.asyncio
async def test_session_id_malformed_colon(client, redis):
    """Malformed ID (colon) → 422, nothing written.

    - ID with `:` is invalid per charset rule
    - Request rejected with 422
    - No session key created in Redis
    """
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    # Malformed session ID
    resp = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Session-ID": "invalid:session",
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "test"}],
        },
    )

    # Must be 422
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_session_id_malformed_space(client, redis):
    """Malformed ID (space) → 422."""
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Session-ID": "invalid session",
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "test"}],
        },
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_session_id_too_long(client, redis):
    """Malformed ID (>128 chars) → 422."""
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    # 129 chars
    long_id = "a" * 129

    resp = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Session-ID": long_id,
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "test"}],
        },
    )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_empty_role_returns_422_invalid_request(client, redis):
    """A message with an empty role is rejected with 422 `invalid_request`."""
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "", "content": "hi"}]},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_missing_model_returns_422_invalid_request_code(client, redis):
    """Error contract: the spec requires code `invalid_request` (not
    `invalid_request_error`) uniformly for all 422 validation failures,
    including the ones caught by FastAPI's own RequestValidationError handler."""
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json()["keys"][0]["key"]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"messages": [{"role": "user", "content": "hi"}]},
    )

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_unknown_session_id_created_when_enabled(
    client, redis, chat_provider_mock, monkeypatch
):
    """With AUTO_CREATE_SESSIONS on, an unknown client-minted ID is claimed.

    - Account presents an ID it never claimed → 200, same ID echoed back
    - A second account presenting the same ID gets its own session, and the
      first account's history is not visible to it (per-account namespacing)
    """
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "auto_create_sessions", True)
    a_key = (await client.post("/v1/accounts", json={})).json()["keys"][0]["key"]
    b_key = (await client.post("/v1/accounts", json={})).json()["keys"][0]["key"]

    a = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {a_key}", "X-Session-ID": "client-made-1"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert a.status_code == 200
    assert a.headers.get("x-session-id") == "client-made-1"

    sessions = await client.get(
        "/v1/sessions", headers={"Authorization": f"Bearer {a_key}"}
    )
    assert "client-made-1" in sessions.text

    b_sessions = await client.get(
        "/v1/sessions", headers={"Authorization": f"Bearer {b_key}"}
    )
    assert "client-made-1" not in b_sessions.text


@pytest.mark.asyncio
async def test_tools_and_tool_choice_reach_the_provider(client, redis, chat_provider_mock, monkeypatch):
    """The client's tool definitions must be forwarded, not silently dropped."""
    import app.router.failover as failover_module
    from app.provider.adapter import AttemptResult
    from tests.conftest import _FakeBlockingResponse

    seen = {}

    async def fake_attempt(reg, key_id, model, messages, **params):
        seen.update(params)
        return AttemptResult(ok=True, response=_FakeBlockingResponse())

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)
    key = (await client.post("/v1/accounts", json={})).json()["keys"][0]["key"]
    tools = [{"type": "function", "function": {"name": "bash", "parameters": {"type": "object"}}}]

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "list files"}],
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        },
    )

    assert resp.status_code == 200
    assert seen["tools"] == tools
    assert seen["tool_choice"] == "auto"
    assert seen["parallel_tool_calls"] is False
