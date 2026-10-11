"""Edge cases: early stream disconnects, history opt-out on failover, input bounds."""
from __future__ import annotations

import asyncio
import json
import logging
import time

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient
from starlette.requests import ClientDisconnect

import app.router.failover as failover_module
from app.api.chat import _stream_response, _Turn
from app.config import get_settings
from app.main import app
from app.pool.catalog import _parse
from app.pool.models import Provider
from app.provider.adapter import AttemptResult, ProviderAttemptError
from app.router.failover import FailoverTrace
from app.session.policy import acquire_turn_lock
from app.session.store import SessionStore
from app.store.redis_store import RedisStore
from app.usage import store as usage_store


@pytest.fixture
async def redis():
    return FakeRedis(decode_responses=True)


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def _api_key(client) -> str:
    return (await client.post("/v1/accounts", json={})).json()["keys"][0]["key"]


@pytest.mark.asyncio
async def test_disconnect_before_the_stream_starts_releases_the_lock(redis, caplog):
    """Starlette never closes an unstarted body; the response must still finalize."""
    lock = await acquire_turn_lock(redis, "acct", "sess")
    turn = _Turn(
        account_id="acct",
        request_id="req-1",
        settle_id="settle-1",
        model="m",
        stream=True,
        started=time.monotonic(),
        redis=redis,
        session_store=SessionStore(redis),
        session_id="sess",
    )

    async def provider_stream():
        yield None  # never reached

    trace = FailoverTrace(model="m", attempts=(), terminal="success", retry_count=0)
    response = _stream_response(
        turn, AttemptResult(ok=True, response=provider_stream()), trace, [], lock
    )

    async def receive():
        await asyncio.sleep(3600)

    async def send(message):
        raise OSError("client went away")

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}}
    with caplog.at_level(logging.INFO, logger="palimpsest.requests"), pytest.raises(ClientDisconnect):
        await response(scope, receive, send)

    assert not turn.stream_started
    assert await redis.exists(lock.lock_key) == 0
    assert lock.renew_task.done()
    records = [json.loads(r.message) for r in caplog.records if r.name == "palimpsest.requests"]
    assert [r["outcome"] for r in records] == ["stream_interrupted"]
    await usage_store.drain()


@pytest.mark.asyncio
async def test_failover_without_history_does_not_inject_stored_turns(
    client, redis, chat_provider_mock, monkeypatch
):
    api_key = await _api_key(client)
    headers = {"Authorization": f"Bearer {api_key}"}
    first = await client.post(
        "/v1/chat/completions",
        headers=headers,
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "remember me"}]},
    )
    session_id = first.headers["x-session-id"]

    seen: list[list[dict]] = []

    async def fails_then_succeeds(reg, key_id, model, messages, **params):
        seen.append(messages)
        if len(seen) == 1:
            error = ProviderAttemptError(503, "service_unavailable", "busy", Provider.OPENAI)
            return AttemptResult(ok=False, error=error)
        return await original(reg, key_id, model, messages, **params)

    original = failover_module.adapter_attempt
    from app.pool.models import KeyCreate
    from app.pool.registry import PoolRegistry

    await PoolRegistry(redis).create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="second-key")
    )
    monkeypatch.setattr(failover_module, "adapter_attempt", fails_then_succeeds)

    resp = await client.post(
        "/v1/chat/completions",
        headers={**headers, "X-Session-ID": session_id},
        json={
            "model": "gpt-4o-mini",
            "use_history": False,
            "messages": [{"role": "user", "content": "fresh question"}],
        },
    )

    assert resp.status_code == 200
    assert len(seen) == 2
    for messages in seen:
        assert messages == [{"role": "user", "content": "fresh question"}]


@pytest.mark.asyncio
async def test_session_id_length_follows_the_setting(client, redis, chat_provider_mock):
    api_key = await _api_key(client)
    settings = get_settings()
    original = settings.max_session_id_length
    settings.max_session_id_length = 8
    try:
        resp = await client.post(
            "/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "X-Session-ID": "a" * 9},
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
    finally:
        settings.max_session_id_length = original

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_pool_key_id_addressing_another_key_type_is_404(client, redis, monkeypatch):
    from app.store.operator import OperatorStore

    monkeypatch.setenv("PALIMPSEST_GATE_OPERATOR_KEY", "pgw_opk_edgecase")
    await OperatorStore(redis).seed_bootstrap_key("pgw_opk_edgecase")
    headers = {"Authorization": "Bearer pgw_opk_edgecase"}
    await redis.set("pgw:pool:keys:by_digest:abc", "x")

    for method, path in (
        ("GET", "/v1/operator/pool/keys/by_digest:abc"),
        ("POST", "/v1/operator/pool/keys/by_digest:abc/suspend"),
        ("DELETE", "/v1/operator/pool/keys/by_digest:abc"),
    ):
        resp = await client.request(method, path, headers=headers)
        assert resp.status_code == 404, (method, path, resp.text)


@pytest.mark.asyncio
async def test_drain_ignores_tasks_from_a_closed_loop():
    other = asyncio.new_event_loop()
    stale = other.create_future()
    other.close()
    usage_store._pending.add(stale)

    await asyncio.wait_for(usage_store.drain(), timeout=2)

    assert stale not in usage_store._pending


def test_model_catalog_skips_malformed_entries():
    body = {"data": ["not-a-dict", None, {"id": "good-model"}]}
    assert [m["id"] for m in _parse(Provider.OPENAI, body)] == ["good-model"]
