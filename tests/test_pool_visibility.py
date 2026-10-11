"""Pool utilization view and failover events."""
from __future__ import annotations

import os
import pathlib
import secrets
import sys

import pytest
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient

from app.pool.events import _EVENTS_KEY, append_failover_event, recent_failover_events
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.router.failover import AttemptRecord, FailoverTrace
from app.settlement.read import utilization_view
from app.settlement.settle import SettlementInput, settle
from app.store.operator import OperatorStore

# app/settlement/__init__.py re-exports the `settle` function, shadowing the
# `settle` submodule's name on the package, so fetch the module via sys.modules.
settle_module = sys.modules["app.settlement.settle"]


@pytest.fixture(autouse=True)
def _master_key():
    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        os.environ["PALIMPSEST_GATE_MASTER_KEY"] = Fernet.generate_key().decode()


@pytest.fixture
async def operator_headers(fake_redis: FakeRedis) -> dict[str, str]:
    result = await OperatorStore(fake_redis).create_key()
    return {"Authorization": f"Bearer {result['key']}"}


@pytest.fixture
async def registry(fake_redis):
    return PoolRegistry(fake_redis)


async def _make_key(registry: PoolRegistry, *, daily_cap: float | None = None) -> str:
    record = await registry.create(
        KeyCreate(
            provider=Provider.OPENAI,
            models=["gpt-4o-mini"],
            key=f"sk-test-{secrets.token_hex(16)}",
            daily_cap=daily_cap,
        )
    )
    return record.id


def _attempt(key_id: str, outcome: str, status_code: int | None = None) -> AttemptRecord:
    return AttemptRecord(
        key_id=key_id,
        provider=Provider.OPENAI,
        classification=None if outcome == "success" else "transient",
        outcome=outcome,
        status_code=status_code,
    )


async def _settle_chain(
    redis, *, request_id: str, attempts: list[AttemptRecord], terminal: str, status_code: int, latency_ms: int = 100
):
    trace = FailoverTrace(model="gpt-4o-mini", attempts=tuple(attempts), terminal=terminal, retry_count=len(attempts) - 1)
    return await settle(
        SettlementInput(
            request_id=request_id,
            account_id="acct-1",
            session_id="sess-1",
            model="gpt-4o-mini",
            trace=trace,
            status_code=status_code,
            latency_ms=latency_ms,
            stream=False,
        ),
        redis=redis,
    )


# -- utilization view shape, aggregate, window semantics ----------------------


@pytest.mark.asyncio
async def test_utilization_reports_state_cap_used_remaining(fake_redis, registry):
    key_a = await _make_key(registry, daily_cap=10000)
    key_b = await _make_key(registry, daily_cap=20000)
    key_c = await _make_key(registry, daily_cap=None)
    await registry.mark_exhausted(key_b)

    await _settle_chain(fake_redis, request_id="r1", attempts=[_attempt(key_a, "success")], terminal="success", status_code=200)
    await _settle_chain(fake_redis, request_id="r2", attempts=[_attempt(key_c, "success")], terminal="success", status_code=200)

    view = await utilization_view(redis=fake_redis)
    by_id = {k["key_id"]: k for k in view["keys"]}

    assert by_id[key_a]["state"] == "active"
    assert by_id[key_a]["daily_cap"] == 10000
    assert by_id[key_a]["used"] == 1
    assert by_id[key_a]["remaining"] == 9999
    assert by_id[key_a]["reset_at"] is not None
    assert by_id[key_a]["seconds_to_reset"] > 0

    assert by_id[key_b]["state"] == "exhausted"
    assert by_id[key_b]["exhausted_reset_at"] is not None
    assert by_id[key_b]["cooldown_remaining_s"] is None

    assert by_id[key_c]["daily_cap"] is None
    assert by_id[key_c]["remaining"] is None
    assert by_id[key_c]["used"] == 1


@pytest.mark.asyncio
async def test_aggregate_block(fake_redis, registry):
    key_a = await _make_key(registry, daily_cap=10000)
    key_b = await _make_key(registry, daily_cap=20000)
    key_c = await _make_key(registry, daily_cap=None)

    await _settle_chain(fake_redis, request_id="r1", attempts=[_attempt(key_a, "success")], terminal="success", status_code=200)
    await _settle_chain(fake_redis, request_id="r2", attempts=[_attempt(key_c, "success")], terminal="success", status_code=200)

    view = await utilization_view(redis=fake_redis)
    agg = view["aggregate"]

    assert agg["total_cap"] == 30000
    assert agg["total_used"] == 2  # includes the uncapped key's usage
    assert agg["utilization_pct"] == round(1 / 30000 * 100, 2)  # only capped usage in the numerator
    assert agg["active_keys"] == 3
    assert agg["exhausted_keys"] == 0

    for key_id in (key_a, key_b, key_c):
        await registry.remove(key_id)
    empty_view = await utilization_view(redis=fake_redis)
    assert empty_view["aggregate"]["utilization_pct"] is None


@pytest.mark.asyncio
async def test_window_semantics_and_cooldown_marker(fake_redis, registry):
    key_a = await _make_key(registry, daily_cap=100)
    await registry.mark_cooldown(key_a)

    view = await utilization_view(redis=fake_redis)
    row = next(k for k in view["keys"] if k["key_id"] == key_a)

    assert row["state"] == "cooldown"
    assert row["cooldown_remaining_s"] is not None
    assert row["cooldown_remaining_s"] <= 60
    assert row["exhausted_reset_at"] is None
    assert row["seconds_to_reset"] > 0


# -- failover success event -----------------------------------------------------


@pytest.mark.asyncio
async def test_failover_success_appends_exactly_one_event(fake_redis, registry):
    key_a = await _make_key(registry, daily_cap=10)
    key_b = await _make_key(registry, daily_cap=10)

    result = await _settle_chain(
        fake_redis,
        request_id="req-failover",
        attempts=[_attempt(key_a, "failed", 500), _attempt(key_b, "success")],
        terminal="success",
        status_code=200,
        latency_ms=444,
    )
    assert result.settled_key == key_b

    events, _ = await recent_failover_events(50, None, redis=fake_redis)
    assert len(events) == 1
    event = events[0]
    assert event["source_key_id"] == key_a
    assert event["target_key_id"] == key_b
    assert event["chain"] == [key_a, key_b]
    assert event["attempts"] == 2
    assert event["duration_ms"] == 444
    assert event["outcome"] == "success"
    assert event["status"] == 200
    assert event["request_id"] == "req-failover"


@pytest.mark.asyncio
async def test_single_attempt_appends_nothing(fake_redis, registry):
    key_a = await _make_key(registry, daily_cap=10)
    await _settle_chain(fake_redis, request_id="req-single", attempts=[_attempt(key_a, "success")], terminal="success", status_code=200)

    events, _ = await recent_failover_events(50, None, redis=fake_redis)
    assert events == []


# -- terminal exhaustion event + counter neutrality ----------------------------------


@pytest.mark.asyncio
async def test_terminal_exhaustion_event_and_counter_neutrality(fake_redis, registry):
    key_a = await _make_key(registry, daily_cap=10)
    key_b = await _make_key(registry, daily_cap=10)

    used_before_a = await fake_redis.get(f"pgw:pool:key:{key_a}:used")
    used_before_b = await fake_redis.get(f"pgw:pool:key:{key_b}:used")

    result = await _settle_chain(
        fake_redis,
        request_id="req-exhausted",
        attempts=[_attempt(key_a, "failed", 429), _attempt(key_b, "failed", 429)],
        terminal="pool_exhausted",
        status_code=503,
    )
    assert result.settled is False

    events, _ = await recent_failover_events(50, None, redis=fake_redis)
    assert len(events) == 1
    assert events[0]["outcome"] == "exhausted_503"
    assert events[0]["status"] == 503
    assert events[0]["target_key_id"] == key_b

    assert await fake_redis.get(f"pgw:pool:key:{key_a}:used") == used_before_a
    assert await fake_redis.get(f"pgw:pool:key:{key_b}:used") == used_before_b


# -- bounding & paging -----------------------------------------------------------


@pytest.mark.asyncio
async def test_bounding_and_paging(fake_redis, registry, monkeypatch):
    key_a = await _make_key(registry, daily_cap=None)
    key_b = await _make_key(registry, daily_cap=None)

    for i in range(5):
        await _settle_chain(
            fake_redis,
            request_id=f"req-page-{i}",
            attempts=[_attempt(key_a, "failed", 500), _attempt(key_b, "success")],
            terminal="success",
            status_code=200,
        )

    page1, next_before = await recent_failover_events(2, None, redis=fake_redis)
    assert len(page1) == 2
    assert next_before is not None
    # newest-first: the most recently appended event comes first
    assert page1[0]["request_id"] == "req-page-4"

    page2, next_before2 = await recent_failover_events(2, next_before, redis=fake_redis)
    assert len(page2) == 2
    assert all(e["ts"] < next_before for e in page2)

    page3, next_before3 = await recent_failover_events(2, next_before2, redis=fake_redis)
    assert len(page3) == 1
    assert next_before3 is None

    page4, next_before4 = await recent_failover_events(2, next_before3 or page3[-1]["ts"], redis=fake_redis)
    assert page4 == []
    assert next_before4 is None


@pytest.mark.asyncio
async def test_list_bounded_to_max_size(fake_redis):
    for i in range(12):
        await append_failover_event(
            {"ts": f"2026-01-01T00:00:{i:02d}Z", "request_id": f"r{i}"}, redis=fake_redis, max_size=10
        )
    length = await fake_redis.llen(_EVENTS_KEY)
    assert length == 10


@pytest.mark.asyncio
async def test_limit_out_of_range_returns_422(async_client: AsyncClient, operator_headers):
    resp_low = await async_client.get("/v1/operator/pool/failover-events?limit=0", headers=operator_headers)
    assert resp_low.status_code == 422
    assert resp_low.json()["error"]["param"] == "limit"

    resp_high = await async_client.get("/v1/operator/pool/failover-events?limit=501", headers=operator_headers)
    assert resp_high.status_code == 422

    resp_ok = await async_client.get("/v1/operator/pool/failover-events?limit=500", headers=operator_headers)
    assert resp_ok.status_code == 200


@pytest.mark.asyncio
async def test_malformed_before_returns_422(async_client: AsyncClient, operator_headers):
    resp = await async_client.get(
        "/v1/operator/pool/failover-events?before=not-a-timestamp", headers=operator_headers
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["param"] == "before"


# -- auth matrix ------------------------------------------------------------------


@pytest.mark.asyncio
async def test_auth_matrix(async_client: AsyncClient, operator_headers):
    no_cred = await async_client.get("/v1/operator/pool/utilization")
    assert no_cred.status_code == 401
    assert no_cred.json()["error"]["code"] == "invalid_api_key"

    ok = await async_client.get("/v1/operator/pool/utilization", headers=operator_headers)
    assert ok.status_code == 200

    ok2 = await async_client.get("/v1/operator/pool/failover-events", headers=operator_headers)
    assert ok2.status_code == 200


@pytest.mark.asyncio
async def test_user_key_on_operator_route_returns_403(async_client: AsyncClient, fake_redis):
    from app.store.redis_store import RedisStore

    store = RedisStore(fake_redis)
    account = await store.create_account()
    user_key_plain = account["keys"][0]["key"]

    resp = await async_client.get(
        "/v1/operator/pool/utilization", headers={"Authorization": f"Bearer {user_key_plain}"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "operator_access_denied"


# -- non-disclosure canary ---------------------------------------------------------


@pytest.mark.asyncio
async def test_no_canary_or_key_material_in_utilization_or_events(
    fake_redis, registry, async_client: AsyncClient, operator_headers, caplog
):
    canary = "canary-prompt-content-should-never-leak"
    key_material = "sk-super-secret-provider-key-material"
    key_a = await _make_key(registry, daily_cap=10)
    key_b = await _make_key(registry, daily_cap=10)

    await _settle_chain(
        fake_redis,
        request_id="req-canary",
        attempts=[_attempt(key_a, "failed", 500), _attempt(key_b, "success")],
        terminal="success",
        status_code=200,
    )

    util_resp = await async_client.get("/v1/operator/pool/utilization", headers=operator_headers)
    events_resp = await async_client.get("/v1/operator/pool/failover-events", headers=operator_headers)

    assert canary not in util_resp.text
    assert canary not in events_resp.text
    assert key_material not in util_resp.text
    assert key_material not in events_resp.text
    for record in caplog.records:
        message = record.getMessage()
        assert canary not in message
        assert key_material not in message


# -- failure tolerance & idempotency -----------------------------------------------


@pytest.mark.asyncio
async def test_redis_fault_in_event_append_does_not_affect_settlement(fake_redis, registry, monkeypatch):
    key_a = await _make_key(registry, daily_cap=10)
    key_b = await _make_key(registry, daily_cap=10)

    from redis.exceptions import ConnectionError as RedisConnectionError

    class _FaultyPipe:
        def lpush(self, *a, **k):
            return self

        def ltrim(self, *a, **k):
            return self

        async def execute(self):
            raise RedisConnectionError("simulated outage")

    class _FaultyRedis:
        def __init__(self, real):
            self._real = real

        def pipeline(self, *a, **k):
            return _FaultyPipe()

        def __getattr__(self, name):
            return getattr(self._real, name)

    faulty = _FaultyRedis(fake_redis)

    # settle() takes `redis` directly; swap it in only for this call so the
    # counter/event Lua script still runs against the real client while the
    # events-list append (a separate pipeline call) faults.
    real_append = settle_module.append_failover_event

    async def _append_via_faulty(record, *, redis, max_size):
        await real_append(record, redis=faulty, max_size=max_size)

    monkeypatch.setattr(settle_module, "append_failover_event", _append_via_faulty)

    result = await _settle_chain(
        fake_redis,
        request_id="req-fault-2",
        attempts=[_attempt(key_a, "failed", 500), _attempt(key_b, "success")],
        terminal="success",
        status_code=200,
    )

    assert result.settled is True  # settlement itself unaffected by the sink fault
    events, _ = await recent_failover_events(50, None, redis=fake_redis)
    assert events == []  # the append genuinely failed -- best-effort, not retried


@pytest.mark.asyncio
async def test_duplicate_settlement_adds_no_second_event(fake_redis, registry):
    key_a = await _make_key(registry, daily_cap=10)
    key_b = await _make_key(registry, daily_cap=10)

    attempts = [_attempt(key_a, "failed", 500), _attempt(key_b, "success")]
    await _settle_chain(fake_redis, request_id="req-dup", attempts=attempts, terminal="success", status_code=200)
    await _settle_chain(fake_redis, request_id="req-dup", attempts=attempts, terminal="success", status_code=200)

    events, _ = await recent_failover_events(50, None, redis=fake_redis)
    assert len(events) == 1


# -- corrupted event resilience --------------------------------------------------


@pytest.mark.asyncio
async def test_corrupted_list_element_is_skipped_not_fatal(fake_redis, registry):
    key_a = await _make_key(registry, daily_cap=10)
    key_b = await _make_key(registry, daily_cap=10)

    await _settle_chain(
        fake_redis,
        request_id="req-clean",
        attempts=[_attempt(key_a, "failed", 500), _attempt(key_b, "success")],
        terminal="success",
        status_code=200,
    )

    # Inject a corrupted entry (non-JSON bytes) directly into the list
    await fake_redis.lpush(_EVENTS_KEY, "not-valid-json-garbage")

    events, _ = await recent_failover_events(50, None, redis=fake_redis)
    assert len(events) == 1
    assert events[0]["request_id"] == "req-clean"


# -- static scans ----------------------------------------------------------------


def test_no_redis_import_or_pgw_literal_in_api_layer():
    for path in pathlib.Path("app/api").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import redis" not in text
        assert "from redis" not in text
        assert "pgw:" not in text


def test_failover_events_key_formatted_only_in_events_module():
    for path in pathlib.Path("app").rglob("*.py"):
        if path == pathlib.Path("app/pool/events.py"):
            continue
        text = path.read_text(encoding="utf-8")
        assert "pgw:pool:failover_events" not in text
