"""Session locking through the real chat route, blocking and streaming."""
import asyncio

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
    """Short wait/poll budget so busy-path tests don't burn the real 2s default."""
    import app.api.chat as chat_module

    fast = Settings(session_lock_wait_ms=200, session_lock_poll_ms=20)
    monkeypatch.setattr(chat_module, "get_settings", lambda: fast)
    return fast


@pytest.mark.asyncio
async def test_blocking_busy_returns_423_zero_provider_calls_no_canary_leak(
    client, redis, chat_provider_mock, fast_lock_settings
):
    """A request on a session whose lock is already held gets 423,
    the provider is never invoked for it, and none of the lock holder's
    content leaks into the busy response body or headers."""
    create_resp = await client.post("/v1/accounts", json={})
    account_data = create_resp.json()
    account_id = account_data["id"]
    api_key = account_data["keys"][0]["key"]

    setup_resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "CANARY-holder-secret"}],
        },
    )
    session_id = setup_resp.headers["x-session-id"]
    assert len(chat_provider_mock.received_calls) == 1

    # Hold the lock out-of-band, simulating an in-flight turn on this session.
    store = SessionStore(redis)
    lock = SessionLock(redis)
    lock_key = store._lock_key(account_id, session_id)
    held, holder_token = await lock.acquire(lock_key)
    assert held

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "X-Session-ID": session_id},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "waiter turn"}]},
    )

    assert resp.status_code == 423
    assert resp.json()["error"]["code"] == "session_busy"
    assert resp.headers.get("x-palimpsest-session-busy") == "true"
    assert resp.headers.get("x-palimpsest-retry-after")
    assert resp.headers.get("x-session-id") == session_id

    # The waiter never reached the provider (only the setup turn did).
    assert len(chat_provider_mock.received_calls) == 1

    # Non-disclosure: the holder's canary appears nowhere in the busy response.
    assert "CANARY-holder-secret" not in resp.text
    assert "CANARY-holder-secret" not in str(resp.headers)

    await lock.release(lock_key, holder_token)


@pytest.mark.asyncio
async def test_streaming_busy_returns_423_zero_provider_calls(
    client, redis, chat_provider_mock, fast_lock_settings
):
    """The streaming path also rejects a contended session with 423 and
    zero provider calls."""
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
    assert len(chat_provider_mock.received_calls) == 1

    store = SessionStore(redis)
    lock = SessionLock(redis)
    lock_key = store._lock_key(account_id, session_id)
    held, holder_token = await lock.acquire(lock_key)
    assert held

    resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}", "X-Session-ID": session_id},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "waiter turn"}],
            "stream": True,
        },
    )

    assert resp.status_code == 423
    assert "text/event-stream" not in resp.headers.get("content-type", "")
    assert resp.json()["error"]["code"] == "session_busy"
    assert resp.headers.get("x-palimpsest-session-busy") == "true"
    assert resp.headers.get("x-palimpsest-retry-after")

    # The waiter never reached the provider (only the setup turn did).
    assert len(chat_provider_mock.received_calls) == 1

    await lock.release(lock_key, holder_token)


@pytest.mark.asyncio
async def test_streaming_concurrent_turns_serialize_no_corruption(client, redis, monkeypatch):
    """k=3 concurrent streaming requests on one session serialize through
    the lock: exactly k exchanges appended, each canary once, turn_count +k."""
    import app.router.failover as failover_module
    from app.pool.models import KeyCreate, Provider
    from app.pool.registry import PoolRegistry
    from app.provider.adapter import AttemptResult

    if not __import__("os").environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        __import__("os").environ["PALIMPSEST_GATE_MASTER_KEY"] = Fernet.generate_key().decode()

    registry = PoolRegistry(redis)
    await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="test-provider-key")
    )

    class _Chunk:
        def __init__(self, content):
            self._content = content
            self.choices = [type("C", (), {"delta": type("D", (), {"content": content})()})()]

        def model_dump(self):
            return {
                "id": "chatcmpl-fake",
                "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": {"content": self._content}, "finish_reason": None}],
            }

    async def _delayed_chunk_stream(content):
        await asyncio.sleep(0.03)  # force real overlap between concurrent turns
        yield _Chunk(content)

    async def fake_attempt(reg, key_id, model, messages, **params):
        canary = next(
            (m["content"] for m in reversed(messages) if "CANARY-" in (m.get("content") or "")),
            "reply",
        )
        return AttemptResult(ok=True, response=_delayed_chunk_stream(canary))

    monkeypatch.setattr(failover_module, "adapter_attempt", fake_attempt)

    create_resp = await client.post("/v1/accounts", json={})
    account_data = create_resp.json()
    account_id = account_data["id"]
    api_key = account_data["keys"][0]["key"]

    # Claim the session up front so all k turns target the same existing session.
    setup_resp = await client.post(
        "/v1/chat/completions",
        headers={"Authorization": f"Bearer {api_key}"},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        },
    )
    session_id = setup_resp.headers["x-session-id"]

    async def turn(i):
        return await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "X-Session-ID": session_id},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": f"CANARY-turn-{i}"}],
                "stream": True,
            },
        )

    responses = await asyncio.gather(*[turn(i) for i in range(3)])
    assert all(r.status_code == 200 for r in responses)

    session_store = SessionStore(redis)
    exchanges = await session_store.read_recent(account_id, session_id, count=10)
    # 1 setup turn + 3 concurrent turns.
    assert len(exchanges) == 4

    # Count only the user's own canary turn, not the mocked assistant reply
    # (which echoes the same canary text back and would otherwise double-count).
    canaries = {}
    for exchange in exchanges:
        for msg in exchange.get("messages", []):
            if msg.get("role") != "user":
                continue
            content = msg.get("content") or ""
            if "CANARY-turn-" in content:
                canaries[content] = canaries.get(content, 0) + 1
    for i in range(3):
        assert canaries.get(f"CANARY-turn-{i}") == 1

    metadata = await session_store.get_metadata(account_id, session_id)
    assert int(metadata["turn_count"]) == 4
