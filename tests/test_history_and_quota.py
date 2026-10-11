"""History de-duplication, quota refunds, the sessions API, reset windows, operator keys."""

import json
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

import app.router.failover as failover_module
from app.config import get_settings
from app.context.rebuild import (
    client_sends_history,
    first_attempt_messages,
    turn_to_persist,
)
from app.main import app
from app.pool.events import _EVENTS_KEY, recent_failover_events
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.provider.adapter import AttemptResult, ProviderAttemptError
from app.router.selection import select_candidates
from app.session.store import SessionStore
from app.settlement.admission import admit
from app.store.operator import LastActiveKeyError, OperatorStore
from app.store.redis_store import RedisStore


@pytest.fixture
async def redis():
    return FakeRedis(decode_responses=True)


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


async def _user(client) -> tuple[str, str]:
    body = (await client.post("/v1/accounts", json={})).json()
    return body["id"], body["keys"][0]["key"]


def _auth(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# -- history duplication -----------------------------------------------------------

SYS = {"role": "system", "content": "Be brief."}


def test_history_resending_client_is_detected_but_tool_turns_are_not():
    assert client_sends_history(
        [
            {"role": "user", "content": "a"},
            {"role": "assistant", "content": "b"},
            {"role": "user", "content": "c"},
        ]
    )
    tool_turn = [
        {"role": "user", "content": "weather?"},
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t"}]},
        {"role": "tool", "tool_call_id": "t", "content": "sun"},
    ]
    assert not client_sends_history(tool_turn)


def test_stored_history_is_not_prepended_to_a_client_that_resends_it():
    inbound = [
        SYS,
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
    ]
    stored = [
        {
            "messages": [
                SYS,
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
            ]
        }
    ]
    assert first_attempt_messages(inbound, stored) == inbound
    assert turn_to_persist(inbound) == [SYS, {"role": "user", "content": "c"}]


def test_a_repeated_system_prompt_reaches_the_provider_once():
    stored = [
        {
            "messages": [
                SYS,
                {"role": "user", "content": "a"},
                {"role": "assistant", "content": "b"},
            ]
        },
        {
            "messages": [
                SYS,
                {"role": "user", "content": "c"},
                {"role": "assistant", "content": "d"},
            ]
        },
    ]
    out = first_attempt_messages([SYS, {"role": "user", "content": "e"}], stored)
    assert out == [
        SYS,
        {"role": "user", "content": "a"},
        {"role": "assistant", "content": "b"},
        {"role": "user", "content": "c"},
        {"role": "assistant", "content": "d"},
        {"role": "user", "content": "e"},
    ]


async def test_full_history_client_with_a_session_sends_each_turn_once(
    client, redis, chat_provider_mock
):
    _account, key = await _user(client)
    first = await client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "one"}]},
    )
    session_id = first.headers["X-Session-ID"]
    reply = first.json()["choices"][0]["message"]

    second = await client.post(
        "/v1/chat/completions",
        headers={**_auth(key), "X-Session-ID": session_id},
        json={
            "model": "gpt-4o-mini",
            "messages": [
                {"role": "user", "content": "one"},
                reply,
                {"role": "user", "content": "two"},
            ],
        },
    )
    assert second.status_code == 200
    sent = chat_provider_mock.received_calls[-1]
    assert [m["content"] for m in sent] == ["one", reply["content"], "two"]


# -- quota refunds -----------------------------------------------------------------


async def test_pool_exhaustion_does_not_spend_the_users_daily_cap(
    client, redis, monkeypatch
):
    monkeypatch.setattr(get_settings(), "user_daily_cap", 1)
    account_id, key = await _user(client)
    for _ in range(3):  # no pool keys at all → pool_exhausted every time
        resp = await client.post(
            "/v1/chat/completions",
            headers=_auth(key),
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "hi"}],
            },
        )
        assert resp.json()["error"]["code"] == "pool_exhausted"
    assert (await admit(account_id, redis=redis)).admitted


async def test_unknown_session_does_not_spend_the_users_daily_cap(
    client, redis, monkeypatch
):
    monkeypatch.setattr(get_settings(), "user_daily_cap", 1)
    account_id, key = await _user(client)
    resp = await client.post(
        "/v1/chat/completions",
        headers={**_auth(key), "X-Session-ID": "not-mine"},
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 404
    assert (await admit(account_id, redis=redis)).admitted


async def test_a_clients_own_bad_request_stays_charged(client, redis, monkeypatch):
    monkeypatch.setattr(get_settings(), "user_daily_cap", 1)
    await PoolRegistry(redis).create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="sk-pool-key-1")
    )

    async def bad_request(*args, **kwargs):
        return AttemptResult(
            ok=False,
            error=ProviderAttemptError(400, "bad", "malformed", Provider.OPENAI),
        )

    monkeypatch.setattr(failover_module, "adapter_attempt", bad_request)
    account_id, key = await _user(client)
    resp = await client.post(
        "/v1/chat/completions",
        headers=_auth(key),
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    assert resp.status_code == 400
    assert not (await admit(account_id, redis=redis)).admitted


# -- user session endpoints --------------------------------------------------------


async def test_users_can_list_and_delete_only_their_own_sessions(
    client, redis, chat_provider_mock
):
    _a, key_a = await _user(client)
    _b, key_b = await _user(client)
    resp = await client.post(
        "/v1/chat/completions",
        headers=_auth(key_a),
        json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
    )
    session_id = resp.headers["X-Session-ID"]

    listed = (await client.get("/v1/sessions", headers=_auth(key_a))).json()["data"]
    assert [s["id"] for s in listed] == [session_id]
    assert listed[0]["turn_count"] == 1
    assert (await client.get("/v1/sessions", headers=_auth(key_b))).json()["data"] == []

    foreign = await client.delete(f"/v1/sessions/{session_id}", headers=_auth(key_b))
    assert foreign.status_code == 404

    deleted = await client.delete(f"/v1/sessions/{session_id}", headers=_auth(key_a))
    assert deleted.status_code == 204
    assert (await client.get("/v1/sessions", headers=_auth(key_a))).json()["data"] == []
    gone = await client.post(
        "/v1/chat/completions",
        headers={**_auth(key_a), "X-Session-ID": session_id},
        json={
            "model": "gpt-4o-mini",
            "messages": [{"role": "user", "content": "again"}],
        },
    )
    assert gone.status_code == 404


# -- operator daily-cap override ---------------------------------------------------


async def test_operator_can_raise_one_accounts_cap_for_today(
    client, redis, monkeypatch
):
    monkeypatch.setattr(get_settings(), "user_daily_cap", 1)
    account_id, _key = await _user(client)
    operator_key = (await OperatorStore(redis).create_key())["key"]

    assert (await admit(account_id, redis=redis)).admitted
    assert not (await admit(account_id, redis=redis)).admitted

    resp = await client.post(
        f"/operator/v1/accounts/{account_id}/daily-cap",
        headers=_auth(operator_key),
        json={"daily_cap": 5},
    )
    assert resp.status_code == 200
    assert datetime.fromisoformat(resp.json()["expires_at"]) > datetime.now(UTC)
    assert (await admit(account_id, redis=redis)).admitted

    missing = await client.post(
        "/operator/v1/accounts/00000000-0000-7000-8000-000000000000/daily-cap",
        headers=_auth(operator_key),
        json={"daily_cap": 5},
    )
    assert missing.status_code == 404


# -- reset hint, selection window --------------------------------------------------


async def test_next_reset_is_in_the_future_after_the_anchor_has_passed(redis):
    registry = PoolRegistry(redis)
    record = await registry.create(
        KeyCreate(
            provider=Provider.OPENAI,
            models=["m"],
            key="sk-reset-key-1",
            reset_at=datetime.now(UTC) + timedelta(hours=1),
            reset_period_hours=24,
        )
    )
    later = record.reset_at + timedelta(days=3, hours=2)
    nxt = record.next_reset_at(later)
    assert later < nxt <= later + timedelta(hours=24)
    assert (nxt - record.reset_at) % timedelta(hours=24) == timedelta(0)


async def test_a_counter_from_the_previous_window_does_not_exclude_a_reset_key(redis):
    registry = PoolRegistry(redis)
    record = await registry.create(
        KeyCreate(
            provider=Provider.OPENAI, models=["m"], key="sk-capped-key", daily_cap=5
        )
    )
    previous_window = record.reset_at.timestamp() - 2 * 86400
    await redis.set(f"pgw:pool:key:{record.id}:used", 5)
    await redis.set(f"pgw:pool:key:{record.id}:used_reset_at", previous_window)

    candidates = await select_candidates(registry, "m")
    assert [c.key_id for c in candidates] == [record.id]
    assert candidates[0].remaining_cap == 5


# -- failover events paging --------------------------------------------------------


async def test_event_pages_are_complete_across_scan_chunks(redis):
    for i in range(450):
        await redis.lpush(_EVENTS_KEY, json.dumps({"ts": f"2026-01-01T00:00:{i:06d}"}))
    seen, before = [], None
    while True:
        page, before = await recent_failover_events(100, before, redis=redis)
        seen.extend(e["ts"] for e in page)
        if before is None:
            break
    assert len(seen) == 450 and seen == sorted(seen, reverse=True)


# -- session index sweep, operator keys --------------------------------------------


async def test_session_index_sweep_runs_at_most_once_per_interval(redis):
    store = SessionStore(redis)
    await store.create_or_claim("acc", "live")
    await store.create_or_claim("acc", "dead")
    await redis.delete(store._session_key("acc", "dead"))

    await store.get_metadata("acc", "live")
    assert await redis.smembers(store._sessions_index_key("acc")) == {"live"}

    await store.create_or_claim("acc", "dead2")
    await redis.delete(store._session_key("acc", "dead2"))
    await store.get_metadata("acc", "live")  # inside the interval: no sweep
    assert "dead2" in await redis.smembers(store._sessions_index_key("acc"))


async def test_last_active_operator_key_cannot_be_revoked(redis):
    store = OperatorStore(redis)
    first = await store.create_key()
    second = await store.create_key()
    assert await store.revoke_key(first["id"]) is True
    with pytest.raises(LastActiveKeyError):
        await store.revoke_key(second["id"])
    assert await store.revoke_key("no-such-key") is False


async def test_operator_auth_records_last_used(client, redis):
    store = OperatorStore(redis)
    key = await store.create_key()
    await redis.hset(
        f"pgw:operator:api_key:{key['id']}", "last_used_at", "2000-01-01T00:00:00"
    )
    await client.get("/operator/v1/keys", headers=_auth(key["key"]))
    last_used = await redis.hget(f"pgw:operator:api_key:{key['id']}", "last_used_at")
    assert last_used > "2026"
