"""A key's human name: set on create, edited, cleared, and shown in the utilization view."""
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore

BASE = "/v1/operator/pool/keys"


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    import os

    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        monkeypatch.setenv("PALIMPSEST_GATE_MASTER_KEY", Fernet.generate_key().decode())


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def headers(redis):
    return {"Authorization": f"Bearer {(await OperatorStore(redis).create_key())['key']}"}


async def _add(client, headers, **extra):
    body = {"provider": "openrouter", "models": ["a/b:free"], "key": "sk-or-abcdefgh1234", **extra}
    return await client.post(BASE, headers=headers, json=body)


@pytest.mark.asyncio
async def test_label_is_stored_trimmed_and_returned(client, headers):
    created = await _add(client, headers, label="  Groq main  ")

    assert created.status_code == 201
    assert created.json()["label"] == "Groq main"
    assert (await client.get(f"{BASE}/{created.json()['id']}", headers=headers)).json()["label"] == "Groq main"


@pytest.mark.asyncio
async def test_blank_or_missing_label_is_none(client, headers):
    assert (await _add(client, headers)).json()["label"] is None
    other = await client.post(
        BASE,
        headers=headers,
        json={"provider": "openai", "models": ["m"], "key": "sk-other-material", "label": "   "},
    )
    assert other.json()["label"] is None


@pytest.mark.asyncio
async def test_label_can_be_renamed_and_cleared_and_is_length_limited(client, headers):
    key_id = (await _add(client, headers, label="Old")).json()["id"]

    renamed = await client.patch(f"{BASE}/{key_id}", headers=headers, json={"label": "New name"})
    assert renamed.json()["label"] == "New name"
    cleared = await client.patch(f"{BASE}/{key_id}", headers=headers, json={"label": None})
    assert cleared.json()["label"] is None
    too_long = await client.patch(f"{BASE}/{key_id}", headers=headers, json={"label": "x" * 61})
    assert too_long.status_code == 422


@pytest.mark.asyncio
async def test_utilization_view_carries_what_a_person_needs_to_recognise_a_key(client, headers):
    await _add(client, headers, label="Free tier")

    (row,) = (await client.get("/v1/operator/pool/utilization", headers=headers)).json()["keys"]

    assert row["label"] == "Free tier"
    assert row["provider"] == "openrouter"
    assert row["models"] == ["a/b:free"]
    assert row["key_hint"].startswith("sk-") and "abcdefgh1234" not in row["key_hint"]


@pytest.mark.asyncio
async def test_dashboard_bundle_shows_names_not_ids(client):
    js = (await client.get("/dashboard/app.js")).text

    assert "function keyName(" in js
    # The by-key table and the failover events show names, not raw ids.
    assert "'Requests this period'" in js and "'Tokens this period'" in js
    assert "failoverKeyName(" in js
    assert "label: 'Key ID'" not in js
