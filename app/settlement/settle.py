"""Settlement: the only writer of the per-key used counter and the request event.

Every request that reaches a terminal state is settled exactly once, here.
``settle()`` never raises: a failure is logged (ids only) and reported as
``SettlementResult(settled=False, ...)``.
"""
from __future__ import annotations

import json
import logging
import pathlib
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from redis.asyncio import Redis

from app.config import get_settings
from app.pool.events import append_failover_event, build_failover_event
from app.pool.models import window_bounds
from app.pool.registry import PoolRegistry
from app.router.failover import FailoverTrace

logger = logging.getLogger("palimpsest.settlement")

_LUA_PATH = pathlib.Path(__file__).parent / "settlement.lua"
_LUA_SRC = _LUA_PATH.read_text(encoding="utf-8")


def _used_key(key_id: str) -> str:
    return f"pgw:pool:key:{key_id}:used"


def _used_reset_at_key(key_id: str) -> str:
    return f"pgw:pool:key:{key_id}:used_reset_at"


def _event_key(request_id: str) -> str:
    return f"pgw:ops:request:{request_id}"


def _settled_marker_key(request_id: str) -> str:
    return f"pgw:ops:settled:{request_id}"


def _as_aware_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def _now() -> datetime:
    """Wall-clock now; patched by tests to cross reset boundaries."""
    return datetime.now(UTC)


def _window_bounds(reset_at: datetime, reset_period_hours: int, now: datetime) -> tuple[float, float]:
    """Current window's ``[start, next_reset)`` as unix timestamps."""
    return window_bounds(reset_at, reset_period_hours, now)


@dataclass(frozen=True)
class SettlementInput:
    """What the chat handler settles once the terminal response is ready."""

    request_id: str
    account_id: str
    session_id: str | None
    model: str
    trace: FailoverTrace
    status_code: int
    latency_ms: int
    stream: bool
    reported_cost: int | None = None


@dataclass(frozen=True)
class SettlementResult:
    """Outcome of one ``settle()`` call."""

    settled_key: str | None
    settled: bool
    event_key: str
    new_used: int | None


def _attribution(trace: FailoverTrace) -> tuple[str | None, list[str]]:
    """``settled_key`` = the last successful attempt's key; ``chain`` = all key_ids in order."""
    chain = [a.key_id for a in trace.attempts]
    settled_key = None
    for attempt in trace.attempts:
        if attempt.outcome == "success":
            settled_key = attempt.key_id
    return settled_key, chain


def _delta(reported_cost: int | None) -> int:
    if reported_cost is None:
        return 1
    return max(0, reported_cost - 1)


async def settle(inp: SettlementInput, *, redis: Redis) -> SettlementResult:
    """Settle one request: exactly-once counter increment and event write."""
    event_key = _event_key(inp.request_id)

    try:
        settled_key, chain = _attribution(inp.trace)
        delta = _delta(inp.reported_cost) if settled_key is not None else 0

        daily_cap = -1.0
        window_start = 0.0
        next_reset = 0.0
        used_key = _used_key(settled_key or "__none__")
        used_reset_at_key = _used_reset_at_key(settled_key or "__none__")

        if settled_key is not None and delta > 0:
            registry = PoolRegistry(redis)
            record = await registry.get(settled_key)
            if record is None:
                # The key was removed after serving; no cap data, no increment.
                delta = 0
            else:
                daily_cap = -1.0 if record.daily_cap is None else float(record.daily_cap)
                window_start, next_reset = _window_bounds(
                    record.reset_at, record.reset_period_hours, _now()
                )

        event_base = {
            "timestamp": time.time(),
            "request_id": inp.request_id,
            "account_id": inp.account_id,
            "session_id": inp.session_id,
            "model": inp.model,
            "chain": chain,
            "attempts": len(inp.trace.attempts),
            "outcome": inp.trace.terminal,
            "status_code": inp.status_code,
            "latency_ms": inp.latency_ms,
            "stream": inp.stream,
        }
        # The script decides whether the cap guard blocks the increment and
        # picks the matching event, so event and counter always agree.
        event_if_settled = {**event_base, "settled_key": settled_key, "settled": 1}
        event_if_not_settled = {**event_base, "settled_key": None, "settled": 0}

        script = redis.register_script(_LUA_SRC)
        now_dt = _now()
        now = now_dt.timestamp()
        moved, new_used = await script(
            keys=[
                used_key,
                used_reset_at_key,
                event_key,
                _settled_marker_key(inp.request_id),
            ],
            args=[
                now,
                window_start,
                next_reset,
                daily_cap,
                delta,
                json.dumps(event_if_settled),
                get_settings().request_log_ttl_hours * 3600,
                get_settings().settle_id_ttl_seconds,
                json.dumps(event_if_not_settled),
            ],
        )

        result = (
            SettlementResult(
                settled_key=settled_key, settled=True, event_key=event_key, new_used=int(new_used)
            )
            if moved == 1
            else SettlementResult(settled_key=None, settled=False, event_key=event_key, new_used=None)
        )

        # A request no provider served gives its admission unit back. A
        # ``fatal`` rejection is the client's own bad request and stays charged.
        # Duplicates never refund twice.
        client_fault = inp.trace.terminal == "fatal" and bool(inp.trace.attempts)
        if moved != -1 and settled_key is None and not client_fault:
            from app.settlement.admission import refund

            await refund(inp.account_id, redis=redis)

        # Record a failover (two or more attempts), once per request. A failure
        # here must not make the committed settlement look failed.
        if moved != -1 and len(chain) >= 2:
            try:
                failover_record = build_failover_event(
                    request_id=inp.request_id,
                    account_id=inp.account_id,
                    session_id=inp.session_id,
                    model=inp.model,
                    chain=chain,
                    status_code=inp.status_code,
                    latency_ms=inp.latency_ms,
                    terminal=inp.trace.terminal,
                )
                await append_failover_event(
                    failover_record, redis=redis, max_size=get_settings().failover_event_log_size
                )
            except Exception:
                logger.error(
                    json.dumps(
                        {"event": "failover_event_append_failed", "request_id": inp.request_id}
                    )
                )

        return result

    except Exception:  # never raises
        logger.error(
            json.dumps({"event": "settlement_failed", "request_id": inp.request_id}), exc_info=True
        )
        return SettlementResult(settled_key=None, settled=False, event_key=event_key, new_used=None)
