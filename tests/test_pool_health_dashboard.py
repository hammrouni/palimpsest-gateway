"""Dashboard pool health and failover section.

Pins the routes and response shapes it reads, the outcome vocabulary its
success rate is derived from, and the realm boundary that keeps user
identifiers out of an operator view.
"""
from __future__ import annotations

import re

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient

from app.pool.events import append_failover_event, build_failover_event
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.settlement.read import utilization_view
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

UTILIZATION_PATH = "/v1/operator/pool/utilization"
EVENTS_PATH = "/v1/operator/pool/failover-events"

_KEY_FIELDS = (
    "key_id",
    "state",
    "daily_cap",
    "used",
    "remaining",
    "reset_at",
    "seconds_to_reset",
)

_AGGREGATE_FIELDS = (
    "total_cap",
    "total_used",
    "total_remaining",
    "utilization_pct",
    "active_keys",
    "exhausted_keys",
    "cooldown_keys",
    "suspended_keys",
)

_EVENT_FIELDS = (
    "ts",
    "request_id",
    "model",
    "source_key_id",
    "target_key_id",
    "chain",
    "attempts",
    "duration_ms",
    "outcome",
    "status",
)

# build_failover_event's outcome tokens; the success rate is success over all.
_OUTCOMES = {"success", "exhausted_503", "error_5xx", "error_4xx"}

_USER_REALM_FIELD = re.compile(r"\.(account|session)\b")


@pytest.fixture
async def bundle(async_client: AsyncClient) -> str:
    response = await async_client.get("/dashboard/app.js")
    assert response.status_code == 200
    return response.text


@pytest.fixture
async def operator_headers(fake_redis: FakeRedis) -> dict[str, str]:
    return {"Authorization": f"Bearer {(await OperatorStore(fake_redis).create_key())['key']}"}


def _event(index: int, terminal: str, status: int) -> dict:
    return build_failover_event(
        request_id=f"req-{index}",
        account_id=f"acct-{index}",
        session_id=None,
        model="gpt-4o-mini",
        chain=[f"key-{index}a", f"key-{index}b"],
        status_code=status,
        latency_ms=100 + index,
        terminal=terminal,
    )


# -- no new API endpoints ---------------------------------------------------


def test_no_new_api_endpoints(app):
    assert set(app.openapi()["paths"]) == _API_PATHS


# -- utilization --------------------------------------------------------------


@pytest.mark.asyncio
async def test_utilization_carries_every_field_the_summary_and_table_render(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    await PoolRegistry(fake_redis).create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="sk-test-abc123", daily_cap=1000)
    )

    response = await async_client.get(UTILIZATION_PATH, headers=operator_headers)

    assert response.status_code == 200
    body = response.json()
    for field in _KEY_FIELDS:
        assert field in body["keys"][0], field
    for field in _AGGREGATE_FIELDS:
        assert field in body["aggregate"], field


@pytest.mark.asyncio
async def test_uncapped_key_reports_null_capacity_and_does_not_dilute_the_aggregate(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    """`daily_cap: null` / `remaining: null`, and its usage stays out of the
    utilization fraction — there is no cap to divide it by."""
    registry = PoolRegistry(fake_redis)
    await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="sk-capped", daily_cap=1000)
    )
    await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="sk-uncapped")
    )

    response = await async_client.get(UTILIZATION_PATH, headers=operator_headers)
    body = response.json()

    uncapped = next(key for key in body["keys"] if key["daily_cap"] is None)
    assert uncapped["remaining"] is None
    assert body["aggregate"]["total_cap"] == 1000


@pytest.mark.asyncio
async def test_utilization_pct_is_null_when_no_key_is_capped(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    await PoolRegistry(fake_redis).create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key="sk-uncapped")
    )

    response = await async_client.get(UTILIZATION_PATH, headers=operator_headers)
    aggregate = response.json()["aggregate"]

    assert aggregate["utilization_pct"] is None
    assert aggregate["total_remaining"] is None


# -- failover events ---------------------------------------------------------


@pytest.mark.asyncio
async def test_event_carries_every_field_the_table_renders(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    await append_failover_event(
        _event(1, "success", 200),
        redis=fake_redis,
        max_size=1000,
    )

    response = await async_client.get(EVENTS_PATH, headers=operator_headers)

    assert response.status_code == 200
    event = response.json()["events"][0]
    for field in _EVENT_FIELDS:
        assert field in event, field
    assert "next_before" in response.json()


@pytest.mark.asyncio
async def test_events_are_newest_first_and_cursor_paginated(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    for index in range(1, 6):
        await append_failover_event(_event(index, "success", 200), redis=fake_redis, max_size=1000)

    first = await async_client.get(EVENTS_PATH, params={"limit": 2}, headers=operator_headers)
    page = first.json()
    assert len(page["events"]) == 2
    assert page["events"][0]["ts"] > page["events"][1]["ts"]
    assert page["next_before"] == page["events"][-1]["ts"]

    # The cursor is an ISO timestamp ending in "+00:00"; a client that interpolates
    # it raw has that "+" decoded as a space and the next page is rejected.
    second = await async_client.get(
        EVENTS_PATH,
        params={"limit": 2, "before": page["next_before"]},
        headers=operator_headers,
    )
    older = second.json()["events"]
    assert all(event["ts"] < page["next_before"] for event in older)
    assert {event["request_id"] for event in older}.isdisjoint(
        {event["request_id"] for event in page["events"]}
    )


@pytest.mark.asyncio
async def test_raw_interpolated_cursor_is_rejected(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    """Guards the encoding rule the dashboard has to get right."""
    for index in range(1, 4):
        await append_failover_event(_event(index, "success", 200), redis=fake_redis, max_size=1000)

    first = await async_client.get(EVENTS_PATH, params={"limit": 2}, headers=operator_headers)
    cursor = first.json()["next_before"]

    raw = await async_client.get(f"{EVENTS_PATH}?limit=2&before={cursor}", headers=operator_headers)

    assert raw.status_code == 422


@pytest.mark.asyncio
async def test_next_before_is_null_at_the_tail(async_client, fake_redis, operator_headers):
    await append_failover_event(_event(1, "success", 200), redis=fake_redis, max_size=1000)

    response = await async_client.get(f"{EVENTS_PATH}?limit=50", headers=operator_headers)

    assert response.json()["next_before"] is None


@pytest.mark.asyncio
async def test_events_reject_an_out_of_range_limit(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    response = await async_client.get(f"{EVENTS_PATH}?limit=0", headers=operator_headers)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


# -- the success rate is derivable from the outcome vocabulary ---------------


@pytest.mark.asyncio
async def test_outcome_vocabulary_is_exactly_the_sm2_basis(
    async_client: AsyncClient, fake_redis: FakeRedis, operator_headers: dict[str, str]
):
    cases = [("success", 200), ("budget_exhausted", 503), ("fatal", 500), ("fatal", 400)]
    for index, (terminal, status) in enumerate(cases, start=1):
        await append_failover_event(
            _event(index, terminal, status), redis=fake_redis, max_size=1000
        )

    response = await async_client.get(f"{EVENTS_PATH}?limit=50", headers=operator_headers)
    events = response.json()["events"]

    assert {event["outcome"] for event in events} == _OUTCOMES

    successful = sum(1 for event in events if event["outcome"] == "success")
    assert successful / len(events) == pytest.approx(0.25)


def test_utilization_view_agrees_with_the_seam_the_section_reads(fake_redis: FakeRedis):
    """The route composes this seam, so the shape cannot drift beneath the UI."""
    import asyncio

    view = asyncio.run(utilization_view(redis=fake_redis))

    assert set(view) == {"keys", "aggregate"}


# -- operator view, no user-realm data --------------------------------------


def test_bundle_never_reads_a_user_realm_identifier(bundle: str):
    """The event log carries `account` and `session`; the operator view
    shows key ids, numbers and state only."""
    assert not _USER_REALM_FIELD.search(bundle)


def test_bundle_binds_to_the_fields_the_section_renders(bundle: str):
    for field in ("utilization_pct", "total_remaining", "seconds_to_reset", "next_before"):
        assert field in bundle, field
    assert "'success'" in bundle
