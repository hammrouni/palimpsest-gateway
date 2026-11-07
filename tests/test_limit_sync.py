import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.pool import limit_sync
from app.pool.limits import LiveLimitsError
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.settlement.read import _used
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore


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


async def _key(redis, provider=Provider.OPENROUTER, cap=50.0, material="sk-or-secret-material"):
    record = await PoolRegistry(redis).create(
        KeyCreate(provider=provider, models=["m"], key=material, daily_cap=cap)
    )
    return record.id


def _live(limit=1000, used=10):
    return {
        "is_free_tier": False,
        "free_model_daily_requests": {"used": used, "limit": limit, "remaining": limit - used},
    }


def _patch_live(monkeypatch, live):
    async def fake(api_key, base_url):
        assert api_key == "sk-or-secret-material"
        return live

    monkeypatch.setattr(limit_sync, "fetch_openrouter", fake)


def test_provider_view_falls_back_to_tier_and_remaining():
    assert limit_sync.provider_view({"is_free_tier": True})["limit"] == 50
    assert limit_sync.provider_view({"is_free_tier": False})["limit"] == 1000
    view = limit_sync.provider_view({"free_model_daily_requests": {"limit": 50, "remaining": 8}})
    assert (view["limit"], view["used"]) == (50, 42)
    assert limit_sync.provider_view({})["limit"] is None


@pytest.mark.asyncio
async def test_drift_is_fixed_locally(redis, monkeypatch):
    _patch_live(monkeypatch, _live(limit=1000, used=10))
    registry = PoolRegistry(redis)
    key_id = await _key(redis, cap=50.0)

    result = await limit_sync.sync_key(registry, key_id, "https://x")

    assert result["status"] == "updated" and result["applied"] is True
    assert {c["field"] for c in result["changes"]} == {"daily_cap", "used"}
    record = await registry.get(key_id)
    assert record.daily_cap == 1000
    assert await _used(redis, key_id, record) == 10


@pytest.mark.asyncio
async def test_matching_key_is_left_alone(redis, monkeypatch):
    _patch_live(monkeypatch, _live(limit=50, used=0))
    key_id = await _key(redis, cap=50.0)

    result = await limit_sync.sync_key(PoolRegistry(redis), key_id, "https://x")

    assert result["status"] == "ok" and result["changes"] == []


@pytest.mark.asyncio
async def test_report_only_changes_nothing(redis, monkeypatch):
    _patch_live(monkeypatch, _live(limit=1000, used=10))
    registry = PoolRegistry(redis)
    key_id = await _key(redis, cap=50.0)

    result = await limit_sync.sync_key(registry, key_id, "https://x", apply=False)

    assert result["status"] == "drift" and result["applied"] is False
    assert (await registry.get(key_id)).daily_cap == 50


@pytest.mark.asyncio
async def test_provider_failure_changes_nothing(redis, monkeypatch):
    async def failing(api_key, base_url):
        raise LiveLimitsError("Could not reach OpenRouter.")

    monkeypatch.setattr(limit_sync, "fetch_openrouter", failing)
    registry = PoolRegistry(redis)
    key_id = await _key(redis, cap=50.0)

    result = await limit_sync.sync_key(registry, key_id, "https://x")

    assert result["status"] == "error" and "OpenRouter" in result["error"]
    assert (await registry.get(key_id)).daily_cap == 50


@pytest.mark.asyncio
async def test_other_providers_are_unsupported(redis):
    key_id = await _key(redis, provider=Provider.GROQ, cap=None, material="gsk-x")

    result = await limit_sync.sync_key(PoolRegistry(redis), key_id, "https://x")

    assert result["status"] == "unsupported"


@pytest.mark.asyncio
async def test_endpoints_sync_one_and_all(client, redis, operator_headers, monkeypatch):
    _patch_live(monkeypatch, _live(limit=1000, used=10))
    key_id = await _key(redis, cap=50.0)
    await _key(redis, provider=Provider.GROQ, cap=None, material="gsk-x")

    dry = await client.post(
        "/v1/operator/pool/sync-limits?apply=false", headers=operator_headers
    )
    assert dry.status_code == 200
    assert dry.json()["checked"] == 1 and dry.json()["drift"] == 1
    assert (await PoolRegistry(redis).get(key_id)).daily_cap == 50

    one = await client.post(
        f"/v1/operator/pool/keys/{key_id}/sync-limits", headers=operator_headers
    )
    assert one.status_code == 200 and one.json()["status"] == "updated"
    assert "sk-or-secret" not in one.text

    again = await client.post("/v1/operator/pool/sync-limits", headers=operator_headers)
    assert again.json()["updated"] == 0


@pytest.mark.asyncio
async def test_endpoint_unknown_key_and_auth(client, redis, operator_headers):
    missing = await client.post(
        "/v1/operator/pool/keys/nope/sync-limits", headers=operator_headers
    )
    assert missing.status_code == 404
    anon = await client.post("/v1/operator/pool/sync-limits")
    assert anon.status_code == 401
