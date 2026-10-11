"""Runtime configuration management.

Pins the risks of mutating cached settings: a change lost on restart, a bound
not enforced (Pydantic does not validate on assignment), a credential leaking
into a response, and the audit record.
"""
from __future__ import annotations

import json
import logging

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient

from app.config import apply_persisted_overrides, get_settings
from app.store.config import ConfigStore
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore

CONFIG_PATH = "/operator/v1/config"

# The runtime-tunable settings.
_CORE_TUNABLES = {
    "verbatim_turns_n",
    "retry_budget",
    "pool_low_water_mark",
    "user_daily_cap",
    "request_log_prompts",
    "cooldown_seconds",
}

# Plus the rest the Dev Notes classify as operator-tunable.
_TUNABLE_FIELDS = _CORE_TUNABLES | {
    "failover_event_log_size",
    "request_log_ttl_hours",
}

# Credentials and infrastructure that must never cross this boundary.
_NEVER_RETURNED = {
    "redis_url",
    "provider_api_key",
    "provider_model",
    "cors_origins",
    "openrouter_base_url",
    "project_name",
    "gate_master_key",
}


@pytest.fixture(autouse=True)
def restore_settings():
    """``get_settings()`` is a process-wide cache; a PATCH here must not leak."""
    settings = get_settings()
    before = settings.model_dump()
    yield
    for key, value in before.items():
        setattr(settings, key, value)


@pytest.fixture
async def operator_headers(fake_redis: FakeRedis) -> dict[str, str]:
    return {"Authorization": f"Bearer {(await OperatorStore(fake_redis).create_key())['key']}"}


async def _user_headers(fake_redis: FakeRedis) -> dict[str, str]:
    account = await RedisStore(fake_redis).create_account("a user")
    return {"Authorization": f"Bearer {account['keys'][0]['key']}"}


# -- realm boundary ----------------------------------------------------------


@pytest.mark.parametrize("method", ["GET", "PATCH"])
@pytest.mark.asyncio
async def test_config_requires_an_operator_key(
    async_client: AsyncClient, fake_redis: FakeRedis, method: str
):
    anonymous = await async_client.request(method, CONFIG_PATH, json={})
    assert anonymous.status_code == 401

    user_realm = await async_client.request(
        method, CONFIG_PATH, json={}, headers=await _user_headers(fake_redis)
    )
    assert user_realm.status_code == 403
    assert user_realm.json()["error"]["code"] == "operator_access_denied"


# -- what GET returns --------------------------------------------------------


@pytest.mark.asyncio
async def test_get_returns_the_tunable_settings(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    response = await async_client.get(CONFIG_PATH, headers=operator_headers)

    assert response.status_code == 200
    body = response.json()
    assert _CORE_TUNABLES <= set(body)
    assert set(body) == _TUNABLE_FIELDS


@pytest.mark.asyncio
async def test_get_never_returns_credentials_or_infrastructure(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    response = await async_client.get(CONFIG_PATH, headers=operator_headers)

    assert _NEVER_RETURNED.isdisjoint(response.json())


@pytest.mark.asyncio
async def test_get_reflects_the_current_settings(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    settings = get_settings()

    response = await async_client.get(CONFIG_PATH, headers=operator_headers)

    assert response.json()["verbatim_turns_n"] == settings.verbatim_turns_n


# -- patching takes effect on the next request -------------------------------


@pytest.mark.asyncio
async def test_patch_updates_only_the_keys_sent(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    before = get_settings().retry_budget

    response = await async_client.patch(
        CONFIG_PATH, json={"verbatim_turns_n": 6}, headers=operator_headers
    )

    assert response.status_code == 200
    assert response.json()["verbatim_turns_n"] == 6
    assert response.json()["retry_budget"] == before
    assert get_settings().verbatim_turns_n == 6


@pytest.mark.asyncio
async def test_patch_is_visible_to_the_next_request_without_a_restart(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    """The cached Settings instance is mutated in place, so a consumer that calls
    ``get_settings()`` after the PATCH sees the new value."""
    await async_client.patch(
        CONFIG_PATH, json={"pool_low_water_mark": 500}, headers=operator_headers
    )

    assert get_settings().pool_low_water_mark == 500


@pytest.mark.asyncio
async def test_patch_survives_a_fresh_settings_instance(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    """A restart rebuilds Settings from the environment, so the override has to be
    persisted and re-applied — not only held in memory."""
    await async_client.patch(
        CONFIG_PATH,
        json={"verbatim_turns_n": 7, "user_daily_cap": 250, "request_log_prompts": True},
        headers=operator_headers,
    )

    overrides = await ConfigStore(fake_redis).get_overrides()

    assert overrides["verbatim_turns_n"] == 7
    assert overrides["user_daily_cap"] == 250
    assert overrides["request_log_prompts"] is True


@pytest.mark.asyncio
async def test_stored_overrides_round_trip_their_types(
    fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    """Redis stores strings; int, bool and null must not come back as text."""
    store = ConfigStore(fake_redis)
    values = {
        "verbatim_turns_n": 6,
        "cooldown_seconds": 120,
        "request_log_prompts": True,
        "user_daily_cap": None,
    }

    await store.set_overrides(values, operator_id="operator-123")
    loaded = await store.get_overrides()

    assert loaded == values
    assert isinstance(loaded["verbatim_turns_n"], int)
    assert isinstance(loaded["cooldown_seconds"], int)
    assert loaded["request_log_prompts"] is True
    assert loaded["user_daily_cap"] is None


# -- validation ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "bad"),
    [
        ("retry_budget", 0),
        ("verbatim_turns_n", 0),
        ("pool_low_water_mark", -1),
        ("user_daily_cap", 0),
        ("cooldown_seconds", 0),
        ("failover_event_log_size", 0),
        ("request_log_ttl_hours", 0),
    ],
)
@pytest.mark.asyncio
async def test_out_of_bounds_values_are_rejected_with_the_offending_field(
    async_client: AsyncClient, operator_headers: dict[str, str], field: str, bad
):
    response = await async_client.patch(
        CONFIG_PATH, json={field: bad}, headers=operator_headers
    )

    assert response.status_code == 422
    assert response.json()["error"]["param"] == field


@pytest.mark.asyncio
async def test_a_rejected_patch_leaves_the_setting_untouched(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    before = get_settings().retry_budget

    await async_client.patch(
        CONFIG_PATH, json={"retry_budget": 0, "verbatim_turns_n": 9}, headers=operator_headers
    )

    assert get_settings().retry_budget == before
    assert get_settings().verbatim_turns_n != 9


@pytest.mark.asyncio
async def test_null_user_daily_cap_is_accepted(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    get_settings().user_daily_cap = 100

    response = await async_client.patch(
        CONFIG_PATH, json={"user_daily_cap": None}, headers=operator_headers
    )

    assert response.status_code == 200
    assert get_settings().user_daily_cap is None


@pytest.mark.asyncio
async def test_unknown_keys_are_ignored(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    response = await async_client.patch(
        CONFIG_PATH,
        json={"not_a_setting": "whatever", "verbatim_turns_n": 5},
        headers=operator_headers,
    )

    assert response.status_code == 200
    assert "not_a_setting" not in response.json()
    assert get_settings().verbatim_turns_n == 5


@pytest.mark.asyncio
async def test_a_non_tunable_known_setting_is_refused(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    """Silently dropping an attempt to change ``redis_url`` would let an operator
    believe infrastructure was reconfigured when it was not."""
    response = await async_client.patch(
        CONFIG_PATH, json={"redis_url": "redis://elsewhere:6379/0"}, headers=operator_headers
    )

    assert response.status_code == 422
    assert get_settings().redis_url != "redis://elsewhere:6379/0"


# -- audit record ------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_change_is_audited_with_who_what_and_the_values(
    fake_redis: FakeRedis,
    caplog: pytest.LogCaptureFixture,
):
    """Asserted at the store: the audit record must not depend on the response."""
    store = ConfigStore(fake_redis)
    operator_id = "operator-123"

    with caplog.at_level(logging.INFO, logger="palimpsest.config"):
        await store.set_overrides(
            {"verbatim_turns_n": 6}, operator_id=operator_id, previous={"verbatim_turns_n": 4}
        )

    record = json.loads(caplog.records[-1].message)
    assert record["operator_id"] == operator_id
    assert record["changes"] == [{"key": "verbatim_turns_n", "old": 4, "new": 6}]
    assert record["ts"]


@pytest.mark.asyncio
async def test_the_api_route_audits_its_own_change(
    async_client: AsyncClient, operator_headers: dict[str, str], caplog
):
    with caplog.at_level(logging.INFO, logger="palimpsest.config"):
        await async_client.patch(
            CONFIG_PATH, json={"cooldown_seconds": 90}, headers=operator_headers
        )

    assert any(record.name == "palimpsest.config" for record in caplog.records)


@pytest.mark.asyncio
async def test_an_ignored_key_is_not_audited_as_a_change(
    fake_redis: FakeRedis, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.INFO, logger="palimpsest.config"):
        await ConfigStore(fake_redis).set_overrides({}, operator_id="op")

    assert not caplog.records


# -- the dashboard boundary ------------------------------------------


@pytest.mark.asyncio
async def test_a_restart_reapplies_saved_overrides():
    settings = get_settings()
    settings.verbatim_turns_n = 4

    apply_persisted_overrides({"verbatim_turns_n": 6, "cooldown_seconds": 90})

    assert get_settings().verbatim_turns_n == 6
    assert get_settings().cooldown_seconds == 90


@pytest.mark.asyncio
async def test_a_stale_override_cannot_write_a_non_tunable_setting():
    """A hash entry left by an older build must not become a route to a credential."""
    settings = get_settings()
    settings.redis_url = "redis://localhost:6379/0"

    applied = apply_persisted_overrides({"redis_url": "redis://elsewhere:6379/0", "nope": 1})

    assert applied == {}
    assert settings.redis_url == "redis://localhost:6379/0"


@pytest.mark.asyncio
async def test_dashboard_assets_are_not_served_stale(async_client: AsyncClient):
    """A cached bundle would serve stale code after an update."""
    for asset in ("index.html", "app.js", "styles.css"):
        response = await async_client.get(f"/dashboard/{asset}")
        assert response.headers.get("Cache-Control") == "no-cache", asset


@pytest.mark.asyncio
async def test_bundle_binds_to_the_config_endpoint(async_client: AsyncClient):
    response = await async_client.get("/dashboard/app.js")
    js = response.text

    assert "'/operator/v1/config'" in js
    assert "PATCH" in js


# -- API surface ---------------------------------------------------------------------


def test_only_the_two_config_routes_were_added(app):
    paths = set(app.openapi()["paths"])

    assert {path for path in paths if path.startswith("/operator/v1/config")} == {CONFIG_PATH}


def test_config_overrides_live_in_the_operator_realm():
    """Operator-realm state must not share the user realm's namespace."""
    assert ConfigStore.CONFIG_HASH.startswith("pgw:operator:")
