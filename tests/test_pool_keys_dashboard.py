"""Dashboard pool key management section.

Pins the API surface it wraps, the record shape it renders, and the
invariants that keep credentials and operator-supplied strings out of reach.
"""
from __future__ import annotations

import re

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient

from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.store.operator import OperatorStore

# Every path the API exposes; a new route must be added here deliberately.
_API_PATHS = {
    "/",
    "/healthz",
    "/operator/v1/accounts",
    "/operator/v1/accounts/{account_id}/daily-cap",
    "/operator/v1/accounts/{account_id}/disable",
    "/operator/v1/accounts/{account_id}/enable",
    "/operator/v1/accounts/{account_id}/keys",
    "/operator/v1/accounts/{account_id}/sessions",
    "/operator/v1/accounts/{account_id}",
    "/operator/v1/config",
    "/operator/v1/requests",
    "/operator/v1/usage",
    "/operator/v1/overview",
    "/operator/v1/stats",
    "/operator/v1/stats/hourly",
    "/operator/v1/accounts/{account_id}/stats",
    "/operator/v1/pricing",
    "/operator/v1/costs",
    "/operator/v1/alerts",
    "/operator/v1/alerts/rules",
    "/operator/v1/alerts/test",
    "/operator/v1/audit",
    "/operator/v1/export/usage.csv",
    "/operator/v1/export/requests.csv",
    "/operator/v1/export/audit.csv",
    "/operator/v1/system",
    "/operator/v1/metrics",
    "/operator/v1/keys",
    "/operator/v1/keys/{key_id}",
    "/v1/accounts",
    "/v1/accounts/{account_id}/keys",
    "/v1/accounts/{account_id}/keys/{key_id}",
    "/v1/chat/completions",
    "/v1/models",
    "/v1/models/{model_id}",
    "/v1/sessions",
    "/v1/sessions/{session_id}",
    "/v1/operator/pool/failover-events",
    "/v1/operator/pool/keys",
    "/v1/operator/pool/keys/{key_id}",
    "/v1/operator/pool/available-models",
    "/v1/operator/pool/keys/{key_id}/available-models",
    "/v1/operator/pool/keys/{key_id}/provider-limits",
    "/v1/operator/pool/keys/{key_id}/resume",
    "/v1/operator/pool/keys/{key_id}/suspend",
    "/v1/operator/pool/utilization",
}

# Method + path for each pool call the section makes.
_POOL_CALLS = {
    ("get", "/v1/operator/pool/keys"),
    ("post", "/v1/operator/pool/keys"),
    ("post", "/v1/operator/pool/keys/{key_id}/suspend"),
    ("post", "/v1/operator/pool/keys/{key_id}/resume"),
    ("delete", "/v1/operator/pool/keys/{key_id}"),
}

# the columns the table renders.
_RENDERED_FIELDS = (
    "id",
    "provider",
    "models",
    "key_hint",
    "state",
    "daily_cap",
    "reset_at",
    "last_modified_by",
)

# The shell names the endpoints the later sections will add, so the bundle may
# reference them before they exist. Anything else absolute must already be served.
_DECLARED_PATHS = _API_PATHS | {"/operator/v1/accounts", "/operator/v1/config"}

_QUOTED_PATH = re.compile(r"""['"](/[^'"]*)['"]""")


@pytest.fixture
async def bundle(async_client: AsyncClient) -> str:
    response = await async_client.get("/dashboard/app.js")
    assert response.status_code == 200
    return response.text


@pytest.fixture
async def operator_headers(fake_redis: FakeRedis) -> dict[str, str]:
    return {"Authorization": f"Bearer {(await OperatorStore(fake_redis).create_key())['key']}"}


# -- no new API endpoints ---------------------------------------------------


def test_no_new_api_endpoints(app):
    assert set(app.openapi()["paths"]) == _API_PATHS


async def test_pool_routes_still_expose_the_calls_the_section_makes(app):
    paths = app.openapi()["paths"]

    assert {(method, path) for path, ops in paths.items() for method in ops} >= _POOL_CALLS


async def test_bundle_calls_only_paths_the_api_serves(bundle: str):
    """A hallucinated or stale endpoint in the bundle fails here, not in a browser.

    Only absolute API-shaped paths are checked: row actions compose `'/suspend'`
    and `'/resume'` as suffixes onto the collection path, which the next test pins.
    """
    called = {
        path
        for path in _QUOTED_PATH.findall(bundle)
        if path.startswith(("/v1/", "/operator/"))
    }

    assert called
    assert called <= _DECLARED_PATHS


def test_row_actions_compose_onto_the_collection_path(bundle: str):
    assert "POOL_KEYS_PATH + '/'" in bundle
    assert "'/suspend'" in bundle
    assert "'/resume'" in bundle
    assert "method: 'DELETE'" in bundle


# -- one authenticated door, nothing logged, nothing injected --------


def test_every_fetch_goes_through_the_shared_authenticated_helper(bundle: str):
    """The login probe plus one helper. A section hand-rolling its own `fetch`
    would slip past the Bearer header, so the count is pinned at two."""
    assert bundle.count("fetch(") == 2


def test_authenticated_helper_attaches_the_operator_key(bundle: str):
    assert re.search(r"function apiFetch", bundle)
    assert "Authorization" in bundle
    assert "Bearer" in bundle


def test_nothing_is_logged(bundle: str):
    """Console output is where submitted key material would leak first."""
    assert "console." not in bundle


def test_no_markup_is_injected_from_api_or_operator_data(bundle: str):
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
        assert sink not in bundle, sink


# -- the record shape the table binds to ----------------------------


@pytest.mark.asyncio
async def test_key_record_carries_every_column_the_table_renders(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    await PoolRegistry(fake_redis).create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="sk-test-abc123")
    )

    response = await async_client.get("/v1/operator/pool/keys", headers=operator_headers)

    assert response.status_code == 200
    record = response.json()[0]
    for field in _RENDERED_FIELDS:
        assert field in record, field
    assert record["state"] in ("active", "exhausted", "cooldown", "suspended")


@pytest.mark.asyncio
async def test_list_response_never_carries_raw_key_material(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    await PoolRegistry(fake_redis).create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="sk-test-abc123")
    )

    response = await async_client.get("/v1/operator/pool/keys", headers=operator_headers)

    assert "key" not in response.json()[0]
    assert "key_material" not in response.json()[0]


# -- the write paths the form and row actions drive -------------------


@pytest.mark.asyncio
async def test_duplicate_key_material_is_rejected_for_the_form_to_surface(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    body = {"provider": "openai", "models": ["gpt-4o-mini"], "key": "sk-test-abc123"}
    first = await async_client.post("/v1/operator/pool/keys", json=body, headers=operator_headers)
    duplicate = await async_client.post("/v1/operator/pool/keys", json=body, headers=operator_headers)

    assert first.status_code == 201
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "duplicate_key"


@pytest.mark.asyncio
async def test_suspend_and_resume_return_the_recorded_state(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    created = await PoolRegistry(fake_redis).create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="sk-test-abc123")
    )

    suspended = await async_client.post(
        f"/v1/operator/pool/keys/{created.id}/suspend", headers=operator_headers
    )
    resumed = await async_client.post(
        f"/v1/operator/pool/keys/{created.id}/resume", headers=operator_headers
    )

    assert suspended.status_code == 200
    assert suspended.json()["state"] == "suspended"
    assert resumed.status_code == 200
    assert resumed.json()["state"] == "active"


@pytest.mark.asyncio
async def test_removing_an_unknown_key_is_a_404_for_the_row_action_to_surface(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    response = await async_client.delete(
        "/v1/operator/pool/keys/does-not-exist", headers=operator_headers
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "key_not_found"


# -- filter and sort are client-side ---------------------------------------


@pytest.mark.asyncio
async def test_filtering_does_not_call_the_api_again(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    """The list route takes no filter parameter; sorting/filtering must not invent one."""
    await PoolRegistry(fake_redis).create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="sk-test-abc123")
    )

    response = await async_client.get("/v1/operator/pool/keys", headers=operator_headers)

    assert response.status_code == 200
    assert set(response.url.query) <= {"limit"}
