"""Provider Key Pool operator routes."""
import logging
import pathlib
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.api.pool_routes import get_pool_registry
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore

# PALIMPSEST_GATE_MASTER_KEY is provided session-wide by tests/conftest.py's
# autouse ``_pool_master_key`` fixture.


@pytest.fixture
async def operator_headers(fake_redis: FakeRedis) -> dict[str, str]:
    result = await OperatorStore(fake_redis).create_key()
    return {"Authorization": f"Bearer {result['key']}"}


# -- add + non-exposure ------------------------------------------------------


@pytest.mark.asyncio
async def test_add_key_returns_201_no_raw_material(
    async_client: AsyncClient, operator_headers: dict[str, str], caplog
):
    caplog.set_level(logging.DEBUG)

    canary = "canary-raw-material-abc123"

    resp = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={"provider": "openai", "models": ["gpt-4o-mini"], "key": canary},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["state"] == "active"
    assert body["provider"] == "openai"
    assert body["operator_id"] is not None
    assert canary not in resp.text
    assert canary not in caplog.text

    list_resp = await async_client.get("/v1/operator/pool/keys", headers=operator_headers)
    assert list_resp.status_code == 200
    ids = [k["id"] for k in list_resp.json()]
    assert body["id"] in ids
    assert canary not in list_resp.text

    get_resp = await async_client.get(
        f"/v1/operator/pool/keys/{body['id']}", headers=operator_headers
    )
    assert get_resp.status_code == 200
    assert canary not in get_resp.text


@pytest.mark.asyncio
async def test_short_key_material_never_disclosed(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    canary = "sk-7chr"  # 7 chars — the <= 7 hint branch

    resp = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={"provider": "openai", "models": ["m"], "key": canary},
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["key_hint"] != canary
    assert canary not in body["key_hint"]
    assert canary not in resp.text

    get_resp = await async_client.get(
        f"/v1/operator/pool/keys/{body['id']}", headers=operator_headers
    )
    assert get_resp.status_code == 200
    assert canary not in get_resp.text
    assert get_resp.json()["key_hint"] == body["key_hint"]

    list_resp = await async_client.get("/v1/operator/pool/keys", headers=operator_headers)
    assert canary not in list_resp.text


@pytest.mark.asyncio
async def test_attribution_exposed_and_updated(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    add = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={"provider": "openai", "models": ["m"], "key": "attr-key"},
    )
    assert add.status_code == 201
    created = add.json()
    assert created["operator_id"] is not None
    assert created["last_modified_by"] == created["operator_id"]

    key_id = created["id"]
    suspended = await async_client.post(
        f"/v1/operator/pool/keys/{key_id}/suspend", headers=operator_headers
    )
    assert suspended.json()["operator_id"] == created["operator_id"]
    assert suspended.json()["last_modified_by"] == created["operator_id"]

    resumed = await async_client.post(
        f"/v1/operator/pool/keys/{key_id}/resume", headers=operator_headers
    )
    assert resumed.json()["last_modified_by"] == created["operator_id"]

    listed = await async_client.get("/v1/operator/pool/keys", headers=operator_headers)
    entry = next(k for k in listed.json() if k["id"] == key_id)
    assert entry["operator_id"] == created["operator_id"]


@pytest.mark.asyncio
async def test_list_is_deterministic_id_ordered(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    for key in ("order-b", "order-a", "order-c"):
        resp = await async_client.post(
            "/v1/operator/pool/keys",
            headers=operator_headers,
            json={"provider": "openai", "models": ["m"], "key": key},
        )
        assert resp.status_code == 201

    listed = await async_client.get("/v1/operator/pool/keys", headers=operator_headers)
    ids = [k["id"] for k in listed.json()]
    assert ids == sorted(ids)

    limited = await async_client.get(
        "/v1/operator/pool/keys", headers=operator_headers, params={"limit": 2}
    )
    assert len(limited.json()) == 2

    bad_limit = await async_client.get(
        "/v1/operator/pool/keys", headers=operator_headers, params={"limit": 0}
    )
    assert bad_limit.status_code == 422


# -- duplicate digest -> 409 --------------------------------------------------


@pytest.mark.asyncio
async def test_add_duplicate_key_returns_409(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    body = {"provider": "openai", "models": ["m"], "key": "dup-material"}
    r1 = await async_client.post("/v1/operator/pool/keys", headers=operator_headers, json=body)
    assert r1.status_code == 201

    r2 = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={**body, "provider": "anthropic"},
    )
    assert r2.status_code == 409

    list_resp = await async_client.get("/v1/operator/pool/keys", headers=operator_headers)
    assert len(list_resp.json()) == 1


# -- suspend/resume immediate + idempotent ------------------------------------


@pytest.mark.asyncio
async def test_suspend_resume_idempotent(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    add = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={"provider": "google", "models": ["gemini"], "key": "sus-key"},
    )
    key_id = add.json()["id"]

    s1 = await async_client.post(
        f"/v1/operator/pool/keys/{key_id}/suspend", headers=operator_headers
    )
    assert s1.status_code == 200
    assert s1.json()["state"] == "suspended"

    s2 = await async_client.post(
        f"/v1/operator/pool/keys/{key_id}/suspend", headers=operator_headers
    )
    assert s2.status_code == 200
    assert s2.json()["state"] == "suspended"

    r1 = await async_client.post(
        f"/v1/operator/pool/keys/{key_id}/resume", headers=operator_headers
    )
    assert r1.status_code == 200
    assert r1.json()["state"] == "active"


# -- operator realm auth, incl. cross-realm 403 ------------------------------


_ALL_POOL_ROUTES = [
    ("POST", "/v1/operator/pool/keys"),
    ("GET", "/v1/operator/pool/keys"),
    ("GET", "/v1/operator/pool/keys/some-key-id"),
    ("POST", "/v1/operator/pool/keys/some-key-id/suspend"),
    ("POST", "/v1/operator/pool/keys/some-key-id/resume"),
    ("DELETE", "/v1/operator/pool/keys/some-key-id"),
]


@pytest.mark.asyncio
async def test_all_six_routes_require_operator_auth(async_client: AsyncClient):
    """All six routes reject a missing credential with 401 and a valid user-realm
    key with 403 — exercised per-route, not just on GET."""
    account_resp = await async_client.post("/v1/accounts", json={})
    raw_user_key = account_resp.json()["keys"][0]["key"]
    user_headers = {"Authorization": f"Bearer {raw_user_key}"}

    for method, url in _ALL_POOL_ROUTES:
        missing = await async_client.request(method, url)
        assert missing.status_code == 401, (method, url, missing.text)
        assert missing.json()["error"]["code"] == "invalid_api_key"

        forbidden = await async_client.request(method, url, headers=user_headers)
        assert forbidden.status_code == 403, (method, url, forbidden.text)
        assert forbidden.json()["error"]["code"] == "operator_access_denied"


@pytest.mark.asyncio
async def test_unknown_id_404_on_get_suspend_resume_delete(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    for method, url in [
        ("GET", "/v1/operator/pool/keys/does-not-exist"),
        ("POST", "/v1/operator/pool/keys/does-not-exist/suspend"),
        ("POST", "/v1/operator/pool/keys/does-not-exist/resume"),
        ("DELETE", "/v1/operator/pool/keys/does-not-exist"),
    ]:
        resp = await async_client.request(method, url, headers=operator_headers)
        assert resp.status_code == 404, (method, url, resp.text)
        assert resp.json()["error"]["code"] == "key_not_found"


# -- delete removes; second delete -> 404 ------------------------------------


@pytest.mark.asyncio
async def test_delete_removes_and_second_delete_404(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    add = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={"provider": "openrouter", "models": ["m"], "key": "del-key"},
    )
    key_id = add.json()["id"]

    d1 = await async_client.delete(
        f"/v1/operator/pool/keys/{key_id}", headers=operator_headers
    )
    assert d1.status_code == 204

    d2 = await async_client.delete(
        f"/v1/operator/pool/keys/{key_id}", headers=operator_headers
    )
    assert d2.status_code == 404

    get_resp = await async_client.get(
        f"/v1/operator/pool/keys/{key_id}", headers=operator_headers
    )
    assert get_resp.status_code == 404


# -- input validation -> 422; daily_cap accepted but not enforced -----------


@pytest.mark.asyncio
async def test_reset_at_in_past_returns_422(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    past = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    resp = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={"provider": "openai", "models": ["m"], "key": "k-past", "reset_at": past},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_naive_reset_at_returns_422(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    resp = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={
            "provider": "openai",
            "models": ["m"],
            "key": "k-naive",
            "reset_at": "2030-01-01T00:00:00",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_empty_models_returns_422(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    resp = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={"provider": "openai", "models": [], "key": "k-empty-models"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["param"] == "models"


@pytest.mark.asyncio
async def test_unknown_provider_returns_422(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    resp = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={"provider": "unknown-provider", "models": ["m"], "key": "k-bad-provider"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["param"] == "provider"


@pytest.mark.asyncio
async def test_missing_or_blank_key_returns_422(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    missing = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={"provider": "openai", "models": ["m"]},
    )
    assert missing.status_code == 422
    assert missing.json()["error"]["param"] == "key"

    blank = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={"provider": "openai", "models": ["m"], "key": "   "},
    )
    assert blank.status_code == 422
    assert blank.json()["error"]["param"] == "key"


@pytest.mark.asyncio
async def test_out_of_range_reset_period_hours_returns_422(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    for bad in (0, 721):
        resp = await async_client.post(
            "/v1/operator/pool/keys",
            headers=operator_headers,
            json={
                "provider": "openai",
                "models": ["m"],
                "key": f"k-period-{bad}",
                "reset_period_hours": bad,
            },
        )
        assert resp.status_code == 422, bad
        assert resp.json()["error"]["param"] == "reset_period_hours"


@pytest.mark.asyncio
async def test_daily_cap_accepted_and_echoed(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    resp = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={
            "provider": "openai",
            "models": ["m"],
            "key": "k-daily-cap",
            "daily_cap": 100,
        },
    )
    assert resp.status_code == 201
    assert resp.json()["daily_cap"] == 100


@pytest.mark.asyncio
async def test_padded_duplicate_material_returns_409(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    resp = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={"provider": "openai", "models": ["m"], "key": "padded-dup"},
    )
    assert resp.status_code == 201

    padded = await async_client.post(
        "/v1/operator/pool/keys",
        headers=operator_headers,
        json={"provider": "anthropic", "models": ["m"], "key": "  padded-dup  "},
    )
    assert padded.status_code == 409

    listed = await async_client.get("/v1/operator/pool/keys", headers=operator_headers)
    assert len(listed.json()) == 1


# -- Outage envelopes: 503 pool_unavailable / 500 store_not_initialized ----------


@pytest.mark.asyncio
async def test_redis_failure_returns_503_pool_unavailable(app, fake_redis):
    app.state.store = RedisStore(fake_redis)
    operator_result = await OperatorStore(fake_redis).create_key()
    headers = {"Authorization": f"Bearer {operator_result['key']}"}

    class _BrokenRegistry:
        async def list(self):
            raise ConnectionError("simulated redis outage")

        async def create(self, *args, **kwargs):
            raise ConnectionError("simulated redis outage")

    app.dependency_overrides[get_pool_registry] = lambda: _BrokenRegistry()
    try:
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://test"
        ) as client:
            list_resp = await client.get("/v1/operator/pool/keys", headers=headers)
            assert list_resp.status_code == 503
            assert list_resp.json()["error"]["code"] == "pool_unavailable"

            add_resp = await client.post(
                "/v1/operator/pool/keys",
                headers=headers,
                json={"provider": "openai", "models": ["m"], "key": "k-outage"},
            )
            assert add_resp.status_code == 503
            assert add_resp.json()["error"]["code"] == "pool_unavailable"
    finally:
        app.dependency_overrides.pop(get_pool_registry, None)


@pytest.mark.asyncio
async def test_pool_route_without_store_returns_500_envelope(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get("/v1/operator/pool/keys")
    assert resp.status_code == 500
    assert resp.json()["error"]["code"] == "store_not_initialized"


# -- no code path in app/api/ decrypts pool key material (static) --------


def test_app_api_never_decrypts_key_material():
    api_dir = pathlib.Path(__file__).resolve().parent.parent / "app" / "api"
    for src in sorted(api_dir.glob("*.py")):
        text = src.read_text()
        assert ".decrypt(" not in text, f"{src.name} calls .decrypt( directly"
    pool_routes_text = (api_dir / "pool_routes.py").read_text()
    assert "get_key_material" not in pool_routes_text
