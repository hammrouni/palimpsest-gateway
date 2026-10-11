"""Operator account deletion: keys, sessions and quota counters go; others stay."""
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.session.store import SessionStore
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore
from app.usage.store import drain


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


async def _account(client) -> tuple[str, dict]:
    body = (await client.post("/v1/accounts", json={})).json()
    return body["id"], {"Authorization": f"Bearer {body['keys'][0]['key']}"}


@pytest.mark.asyncio
async def test_delete_removes_account_keys_sessions_and_counters(
    client, redis, chat_provider_mock, operator_headers
):
    victim_id, victim = await _account(client)
    other_id, other = await _account(client)
    for headers in (victim, other):
        resp = await client.post(
            "/v1/chat/completions",
            headers=headers,
            json={"model": "gpt-4o-mini", "messages": [{"role": "user", "content": "hi"}]},
        )
        assert resp.status_code == 200
    await drain()
    assert await redis.exists(f"pgw:user:{victim_id}:used")

    gone = await client.delete(f"/operator/v1/accounts/{victim_id}", headers=operator_headers)
    assert gone.status_code == 204

    # Its key no longer authenticates.
    assert (await client.get("/v1/sessions", headers=victim)).status_code == 401
    # Nothing of it is left in Redis outside the aggregate usage history.
    leftovers = [
        k.decode() async for k in redis.scan_iter(match=f"*{victim_id}*")
        if not k.decode().startswith("usage:")
    ]
    assert leftovers == []
    assert await SessionStore(redis).list_sessions(victim_id) == []

    accounts = (await client.get("/operator/v1/accounts", headers=operator_headers)).json()
    assert [a["id"] for a in accounts] == [other_id]
    # The other account is untouched.
    assert (await client.get("/v1/sessions", headers=other)).status_code == 200


@pytest.mark.asyncio
async def test_delete_unknown_account_is_404_and_requires_operator(
    client, redis, operator_headers
):
    missing = await client.delete("/operator/v1/accounts/nope", headers=operator_headers)
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "account_not_found"

    account_id, user = await _account(client)
    assert (await client.delete(f"/operator/v1/accounts/{account_id}")).status_code == 401
    assert (
        await client.delete(f"/operator/v1/accounts/{account_id}", headers=user)
    ).status_code in (401, 403)
    # Still there.
    assert (await client.get("/v1/sessions", headers=user)).status_code == 200
