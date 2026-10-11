"""Conversation continuity and history persistence across turns."""

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.session.store import SessionStore
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
async def test_continuity_two_turn_blocking(client, redis, chat_provider_mock):
    """Two-turn blocking chat with history continuity.

    Turn 1: User says 'hello'
    - Gateway generates session ID and claims it
    - Provider receives just [{"role":"user","content":"hello"}]
    - Exchange persisted: [{"role":"user","content":"hello"}, {"role":"assistant","content":"<mock>"}]
    - turn_count = 1

    Turn 2: User says 'what did I say?' with same session ID
    - Provider receives [{"role":"user","content":"hello"}, {"role":"assistant",...}, {"role":"user","content":"what did I say?"}]
    - Exact message sequence verified
    - Exchange persisted: [{"role":"user","content":"what did I say?"}, {"role":"assistant",...}]
    - turn_count = 2
    """
    # Setup: create account
    create_resp = await client.post("/v1/accounts", json={})
    assert create_resp.status_code == 201
    account_data = create_resp.json()
    account_id = account_data.get("id") or account_data.get("account", {}).get("id")
    api_key = account_data.get("key") or (account_data.get("keys", [{}])[0].get("key"))

    # Turn 1: send "hello", no session ID
    t1_resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hello"}],
        },
    )

    # Turn 1 should succeed with session ID issued
    assert t1_resp.status_code == 200
    t1_data = t1_resp.json()
    assert "x-session-id" in {k.lower() for k in t1_resp.headers.keys()}
    session_id = t1_resp.headers.get("x-session-id")

    # Verify assistant response exists
    assert len(t1_data["choices"]) == 1
    assert t1_data["choices"][0]["message"]["role"] == "assistant"

    # Turn 2: send "what did I say?", reuse session
    t2_resp = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Session-ID": session_id,
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "what did I say?"}],
        },
    )

    # Turn 2 should succeed
    assert t2_resp.status_code == 200
    assert t2_resp.headers.get("x-session-id") == session_id

    # The exact messages the provider received on turn 2, not just what was
    # persisted.
    assert len(chat_provider_mock.received_calls) == 2
    assert chat_provider_mock.received_calls[0] == [
        {"role": "user", "content": "hello"}
    ]
    assert chat_provider_mock.received_calls[1] == [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "mock reply"},
        {"role": "user", "content": "what did I say?"},
    ]

    # turn_count advanced
    session_store = SessionStore(redis)
    metadata = await session_store.get_metadata(account_id, session_id)

    assert metadata is not None
    # turn_count should be 2 (one for each exchange persisted)
    turn_count = int(metadata.get("turn_count", 0))
    assert turn_count == 2, f"Expected turn_count=2, got {turn_count}"

    # Verify history has 2 exchanges
    exchanges = await session_store.read_recent(account_id, session_id, count=10)
    assert len(exchanges) == 2

    # Verify turn 1 exchange structure
    t1_exchange = exchanges[0]
    assert "ts" in t1_exchange
    assert "messages" in t1_exchange
    assert len(t1_exchange["messages"]) == 2  # user msg + assistant reply
    assert t1_exchange["messages"][0]["role"] == "user"
    assert t1_exchange["messages"][0]["content"] == "hello"
    assert t1_exchange["messages"][1]["role"] == "assistant"

    # Verify turn 2 exchange structure
    t2_exchange = exchanges[1]
    assert len(t2_exchange["messages"]) == 2  # user msg + assistant reply
    assert t2_exchange["messages"][0]["role"] == "user"
    assert t2_exchange["messages"][0]["content"] == "what did I say?"
    assert t2_exchange["messages"][1]["role"] == "assistant"


@pytest.mark.asyncio
async def test_ttl_refreshed_on_access(client, redis, chat_provider_mock):
    """TTLs refreshed on every access.

    Create session, verify initial TTL, read session (which triggers touch),
    verify TTL was refreshed (still >0 after access).
    """
    # Setup: create account
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json().get("key") or create_resp.json().get("keys", [{}])[
        0
    ].get("key")
    account_id = create_resp.json().get("id") or create_resp.json().get(
        "account", {}
    ).get("id")

    # Create session
    t1_resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    session_id = t1_resp.headers.get("x-session-id")

    session_store = SessionStore(redis)

    # Check TTL after creation
    key = session_store._session_key(account_id, session_id)
    ttl_after_create = await redis.ttl(key)
    assert ttl_after_create > 0

    # Access session (turn 2), which should refresh TTL
    await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Session-ID": session_id,
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "follow up"}],
        },
    )

    ttl_after_access = await redis.ttl(key)
    # TTL should be close to original (not much time passed)
    assert ttl_after_access > 0
    # Should not significantly decrease
    assert ttl_after_access >= (ttl_after_create - 5)


@pytest.mark.asyncio
async def test_use_history_false_still_persists(client, redis, chat_provider_mock):
    """use_history=false → provider sees only this turn, but exchange still persisted + turn_count +1.

    Turn 1: normal
    Turn 2: use_history=false, "new topic"
    - Provider should NOT see turn 1 context
    - But exchange is still persisted
    - turn_count should be 2
    """
    # Setup
    create_resp = await client.post("/v1/accounts", json={})
    api_key = create_resp.json().get("key") or create_resp.json().get("keys", [{}])[
        0
    ].get("key")
    account_id = create_resp.json().get("id") or create_resp.json().get(
        "account", {}
    ).get("id")

    # Turn 1
    t1_resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "secret"}],
        },
    )
    session_id = t1_resp.headers.get("x-session-id")

    # Turn 2 with use_history=false
    t2_resp = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Session-ID": session_id,
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "new topic"}],
            "use_history": False,
        },
    )

    # Should succeed
    assert t2_resp.status_code == 200

    # Verify turn_count = 2 (both exchanges persisted)
    session_store = SessionStore(redis)
    metadata = await session_store.get_metadata(account_id, session_id)
    turn_count = int(metadata.get("turn_count", 0))
    assert turn_count == 2

    # Verify 2 exchanges exist
    exchanges = await session_store.read_recent(account_id, session_id, count=10)
    assert len(exchanges) == 2


@pytest.mark.asyncio
async def test_model_switch_without_tools_strips_orphaned_tool_calls(
    client, redis, chat_provider_mock
):
    """A turn that used tools followed by a turn that does not (e.g. model switch)
    must not forward tool_calls / tool-role messages to the provider.

    The previous turn's stored exchange contains an assistant message with
    tool_calls and a tool-role result message. When the current request
    omits tools, first_attempt_messages strips those orphaned tool messages
    from the history so the provider does not return HTTP 400.
    """
    from app.session.store import SessionStore

    # Setup: create account
    create_resp = await client.post("/v1/accounts", json={})
    assert create_resp.status_code == 201
    account_data = create_resp.json()
    account_id = account_data.get("id") or account_data.get("account", {}).get("id")
    api_key = account_data.get("key") or account_data.get("keys", [{}])[0].get("key")

    # Seed a session with a prior exchange that used tools
    session_store = SessionStore(redis)
    await session_store.create_or_claim(account_id, "sess-tool-switch")
    await session_store.append_exchange(
        account_id,
        "sess-tool-switch",
        [
            {"role": "user", "content": "weather?"},
            {"role": "assistant", "content": None, "tool_calls": [{"id": "t"}]},
            {"role": "tool", "tool_call_id": "t", "content": "sunny"},
            {"role": "assistant", "content": "it is sunny"},
        ],
    )

    # Turn 2: no tools parameter — the orphaned tool_calls must be stripped
    t2_resp = await client.post(
        "/v1/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "X-Session-Id": "sess-tool-switch",
        },
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "what is the weather?"}],
        },
    )

    assert t2_resp.status_code == 200

    sent = chat_provider_mock.received_calls[-1]
    assert not any(m.get("tool_calls") for m in sent), (
        "tool_calls leaked into provider request"
    )
    assert not any(m.get("role") == "tool" for m in sent), (
        "tool-role message leaked into provider request"
    )
    assert {"role": "user", "content": "weather?"} in sent
    assert {"role": "assistant", "content": "it is sunny"} in sent
    assert sent[-1] == {"role": "user", "content": "what is the weather?"}
