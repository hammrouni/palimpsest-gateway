"""Dashboard shell and operator sign-in.

The dashboard is a static bundle served by the API app. These tests pin the
contract it depends on: its own prefix, nothing loaded from elsewhere, its
sections and endpoints, the sign-in outcomes, and no shadowed routes.
"""
from __future__ import annotations

import pathlib
import re

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles

from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# section label -> the operator API surface it is backed by.
_SECTION_ENDPOINTS: list[tuple[str, str]] = [
    ("Pool Keys", "/v1/operator/pool/keys"),
    ("Utilization & Failover", "/v1/operator/pool/utilization"),
    ("Utilization & Failover", "/v1/operator/pool/failover-events"),
    ("Users", "/operator/v1/accounts"),
    ("Configuration", "/operator/v1/config"),
]

# The login form probes this existing operator route with the typed key; the
# status it returns is the whole of the login decision.
_AUTH_PROBE_PATH = "/v1/operator/pool/keys"

# Routes that existed before the dashboard mount was added.
_PRE_EXISTING_ROUTES: list[tuple[str, str]] = [
    ("GET", "/operator/v1/keys"),
    ("GET", "/v1/operator/pool/keys"),
    ("GET", "/v1/operator/pool/utilization"),
    ("GET", "/v1/operator/pool/failover-events"),
    ("GET", "/v1/accounts/some-account/keys"),
    ("POST", "/v1/chat/completions"),
    ("GET", "/healthz"),
    ("GET", "/"),
]


@pytest.fixture
async def operator_key(fake_redis: FakeRedis) -> str:
    return (await OperatorStore(fake_redis).create_key())["key"]


@pytest.fixture
async def user_key(fake_redis: FakeRedis) -> str:
    return (await RedisStore(fake_redis).create_account("a user"))["keys"][0]["key"]


async def _bundle(client: AsyncClient, path: str) -> str:
    response = await client.get(path)
    assert response.status_code == 200, (path, response.status_code)
    return response.text


# -- served by the API process, under its own prefix ------------------------


@pytest.mark.asyncio
async def test_shell_is_served_at_the_dashboard_prefix(async_client: AsyncClient):
    response = await async_client.get("/dashboard/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")


@pytest.mark.asyncio
async def test_bare_prefix_redirects_to_the_shell(async_client: AsyncClient):
    response = await async_client.get("/dashboard", follow_redirects=False)

    assert response.status_code in (301, 302, 307, 308)
    assert response.headers["location"].endswith("/dashboard/")


def test_shell_is_a_static_mount_on_the_api_app(app):
    """One process, one app — the bundle is a mount, not a service."""
    mounts = [r for r in app.routes if isinstance(r, Mount) and r.path == "/dashboard"]

    assert len(mounts) == 1
    assert isinstance(mounts[0].app, StaticFiles)

    assert "/v1/chat/completions" in app.openapi()["paths"]


@pytest.mark.asyncio
async def test_unknown_bundle_asset_is_404(async_client: AsyncClient):
    response = await async_client.get("/dashboard/does-not-exist.js")

    assert response.status_code == 404


# -- no second web server, no CDN ------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("asset", ["index.html", "app.js", "styles.css"])
async def test_bundle_assets_are_served_locally(async_client: AsyncClient, asset: str):
    expected = {"index.html": "text/html", "app.js": "javascript", "styles.css": "text/css"}

    response = await async_client.get(f"/dashboard/{asset}")

    assert response.status_code == 200
    assert expected[asset] in response.headers["content-type"]


@pytest.mark.asyncio
@pytest.mark.parametrize("asset", ["index.html", "app.js", "styles.css"])
async def test_no_asset_references_anything_external(async_client: AsyncClient, asset: str):
    body = await _bundle(async_client, f"/dashboard/{asset}")

    assert not re.search(r"https?://", body), f"{asset} reaches outside the gateway"


@pytest.mark.asyncio
async def test_shell_loads_its_assets_from_the_bundle(async_client: AsyncClient):
    html = await _bundle(async_client, "/dashboard/index.html")

    referenced = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert referenced[-2:] == ["styles.css", "app.js"] or "app.js" in referenced
    # Every reference is a local, relative path inside the bundle.
    for path in referenced:
        assert not path.startswith(("http:", "https:", "//", "/")), path
        response = await async_client.get(f"/dashboard/{path}")
        assert response.status_code == 200, path


def test_static_bundle_ships_in_the_container_image():
    """The bundle lives under app/, which the image copies wholesale."""
    dockerfile = (_REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert "COPY app/ ./app/" in dockerfile


# -- the login decision the shell renders ----------------------------


@pytest.mark.asyncio
async def test_typed_operator_key_authenticates(async_client: AsyncClient, operator_key: str):
    response = await async_client.get(
        _AUTH_PROBE_PATH, headers={"Authorization": f"Bearer {operator_key}"}
    )

    assert response.status_code == 200


@pytest.mark.asyncio
async def test_user_realm_key_is_rejected_as_a_realm_violation(
    async_client: AsyncClient, user_key: str
):
    response = await async_client.get(
        _AUTH_PROBE_PATH, headers={"Authorization": f"Bearer {user_key}"}
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "operator_access_denied"


@pytest.mark.asyncio
async def test_unknown_key_is_rejected_as_a_bad_credential(async_client: AsyncClient):
    response = await async_client.get(
        _AUTH_PROBE_PATH, headers={"Authorization": "Bearer pgw_opk_not-a-real-key"}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_shell_branches_on_both_auth_failures(async_client: AsyncClient):
    js = await _bundle(async_client, "/dashboard/app.js")

    assert re.search(r"\b401\b", js)
    assert re.search(r"\b403\b", js)
    assert "operator_access_denied" in js


@pytest.mark.asyncio
async def test_shell_keeps_the_key_out_of_persistent_storage(async_client: AsyncClient):
    js = await _bundle(async_client, "/dashboard/app.js")

    assert "sessionStorage" in js
    assert "localStorage" not in js


@pytest.mark.asyncio
async def test_shell_sends_the_key_as_a_bearer_header(async_client: AsyncClient):
    js = await _bundle(async_client, "/dashboard/app.js")

    assert re.search(r"Authorization", js)
    assert re.search(r"Bearer", js)


# -- top-level navigation ---------------------------------------------------


@pytest.mark.asyncio
async def test_login_form_and_navigation_mount_points_exist(async_client: AsyncClient):
    html = await _bundle(async_client, "/dashboard/index.html")

    assert "<form" in html
    assert re.search(r'<input[^>]*type="password"', html)
    assert "<nav" in html
    assert "<main" in html


@pytest.mark.asyncio
@pytest.mark.parametrize(("label", "endpoint"), _SECTION_ENDPOINTS)
async def test_section_is_declared_with_its_backing_endpoint(
    async_client: AsyncClient, label: str, endpoint: str
):
    js = await _bundle(async_client, "/dashboard/app.js")

    assert label in js
    assert endpoint in js


@pytest.mark.asyncio
async def test_api_calls_stay_on_the_gateway_origin(async_client: AsyncClient):
    js = await _bundle(async_client, "/dashboard/app.js")

    assert re.search(r"""fetch\(\s*['"]/""", js)


# -- the dashboard is a pure addition ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(("method", "path"), _PRE_EXISTING_ROUTES)
async def test_dashboard_does_not_shadow_existing_routes(
    async_client: AsyncClient, method: str, path: str
):
    response = await async_client.request(method, path, json={} if method == "POST" else None)

    assert response.status_code != 404, path
    assert response.status_code in (200, 401, 422), (path, response.status_code)


@pytest.mark.asyncio
async def test_operator_routes_still_answer_with_their_own_auth(
    async_client: AsyncClient, operator_key: str
):
    """The mount did not absorb /operator/v1/keys — it still authenticates."""
    response = await async_client.get(
        "/operator/v1/keys", headers={"Authorization": f"Bearer {operator_key}"}
    )

    assert response.status_code == 200
    assert isinstance(response.json(), list)


@pytest.mark.asyncio
async def test_root_endpoint_advertises_the_dashboard(async_client: AsyncClient):
    response = await async_client.get("/")

    assert response.status_code == 200
    assert "/dashboard/" in response.json()["endpoints"]
