"""Quota reads for the admission throttle and the operator views.

Reads the per-key used counters written by ``settle()`` and the registry.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from redis.asyncio import Redis

from app.pool.registry import PoolRegistry
from app.pool.tokens import tokens_used
from app.settlement.settle import (
    _event_key,
    _used_key,
    _used_reset_at_key,
    _window_bounds,
)

logger = logging.getLogger("palimpsest.settlement.read")


async def _used(redis: Redis, key_id: str, record=None) -> int:
    """The key's used counter; 0 when corrupt or from an earlier window."""
    raw = await redis.get(_used_key(key_id))
    if raw is None:
        return 0
    try:
        used = int(raw)
    except (TypeError, ValueError):
        logger.warning(json.dumps({"event": "key_counter_corrupt", "key_id": key_id}))
        return 0

    # Only the next settlement resets a counter, so one stamped before the
    # current window reads as 0.
    if record is not None and used:
        raw_ws = await redis.get(_used_reset_at_key(key_id))
        if raw_ws is not None:
            try:
                stored_ws = float(raw_ws)
            except (TypeError, ValueError):
                return used
            window_start, _next = _window_bounds(
                record.reset_at, record.reset_period_hours, datetime.now(UTC)
            )
            if stored_ws < window_start:
                return 0
    return used


async def remaining(key_id: str, *, redis: Redis) -> int | None:
    """``daily_cap - used`` (floor 0) for a capped key; ``None`` for uncapped or unknown."""
    registry = PoolRegistry(redis)
    record = await registry.get(key_id)
    if record is None or record.daily_cap is None:
        return None
    used = await _used(redis, key_id, record)
    return max(0, int(record.daily_cap) - used)


@dataclass(frozen=True)
class KeyUtilization:
    """Per-key utilization snapshot."""

    key_id: str
    daily_cap: float | None
    used: int
    remaining: int | None


@dataclass(frozen=True)
class PoolUtilization:
    """Pool-wide aggregate.

    ``total_cap`` and ``fraction_served`` cover capped keys only;
    ``total_used`` covers every key.
    """

    per_key: list[KeyUtilization]
    total_cap: float | None
    total_used: int
    fraction_served: float | None


async def utilization(*, redis: Redis) -> PoolUtilization:
    """Per-key + aggregate utilization across every registry key."""
    registry = PoolRegistry(redis)
    records = await registry.list()

    per_key: list[KeyUtilization] = []
    total_cap = 0.0
    total_used = 0
    total_used_capped = 0
    any_capped = False

    for record in records:
        used = await _used(redis, record.id, record)
        total_used += used
        rem: int | None
        if record.daily_cap is None:
            rem = None
        else:
            any_capped = True
            total_cap += record.daily_cap
            total_used_capped += used
            rem = max(0, int(record.daily_cap) - used)
        per_key.append(
            KeyUtilization(key_id=record.id, daily_cap=record.daily_cap, used=used, remaining=rem)
        )

    # Capped usage only, so the fraction never exceeds 1.0.
    fraction_served = (total_used_capped / total_cap) if any_capped and total_cap > 0 else None
    return PoolUtilization(
        per_key=per_key,
        total_cap=total_cap if any_capped else None,
        total_used=total_used,
        fraction_served=fraction_served,
    )


async def get_event(request_id: str, *, redis: Redis) -> dict | None:
    """The settlement event for ``request_id``, or ``None`` if absent or expired."""
    raw = await redis.hget(_event_key(request_id), "event")
    if raw is None:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        logger.warning(json.dumps({"event": "event_corrupt", "request_id": request_id}))
        return None


async def utilization_view(*, redis: Redis) -> dict:
    """The operator utilization view: every key's state and capacity plus totals."""
    registry = PoolRegistry(redis)
    pool_util = await utilization(redis=redis)
    states = {s.id: s for s in await registry.list_states()}
    records = {r.id: r for r in await registry.list()}

    now = datetime.now(UTC)
    keys_out: list[dict] = []
    state_counts = {"active": 0, "exhausted": 0, "cooldown": 0, "suspended": 0}
    total_used_capped = 0
    total_tokens_used = 0
    total_token_cap = 0
    total_tokens_left = 0

    for ku in pool_util.per_key:
        state_view = states.get(ku.key_id)
        record = records.get(ku.key_id)
        state = state_view.state.value if state_view else "active"
        state_counts[state] = state_counts.get(state, 0) + 1

        seconds_to_reset = None
        reset_at_iso = None
        next_reset_iso = None
        if record is not None:
            next_reset = record.next_reset_at(now)
            seconds_to_reset = max(0, int((next_reset - now).total_seconds()))
            reset_at_iso = record.reset_at.isoformat()
            next_reset_iso = next_reset.isoformat()

        cooldown_remaining_s = None
        exhausted_reset_at = None
        if state_view is not None and state_view.seconds_remaining is not None:
            if state == "cooldown":
                cooldown_remaining_s = state_view.seconds_remaining
            elif state == "exhausted":
                exhausted_reset_at = (
                    now + timedelta(seconds=state_view.seconds_remaining)
                ).isoformat()

        token_cap = record.token_cap if record is not None else None
        tokens_used_now = await tokens_used(redis, record) if record is not None else 0
        total_tokens_used += tokens_used_now
        if token_cap is not None:
            total_token_cap += token_cap
            total_tokens_left += max(0, token_cap - tokens_used_now)

        if ku.daily_cap is not None:
            total_used_capped += ku.used

        keys_out.append(
            {
                "key_id": ku.key_id,
                "label": record.label if record is not None else None,
                "provider": record.provider.value if record is not None else None,
                "key_hint": record.key_hint if record is not None else None,
                "models": record.models if record is not None else [],
                "state": state,
                "daily_cap": ku.daily_cap,
                "used": ku.used,
                "remaining": ku.remaining,
                "reset_at": reset_at_iso,
                "next_reset_at": next_reset_iso,
                "seconds_to_reset": seconds_to_reset,
                "cooldown_remaining_s": cooldown_remaining_s,
                "exhausted_reset_at": exhausted_reset_at,
                "token_cap": token_cap,
                "tokens_used": tokens_used_now,
                "tokens_remaining": (
                    max(0, token_cap - tokens_used_now) if token_cap is not None else None
                ),
                "cooling_models": state_view.cooling_models if state_view else [],
            }
        )

    total_cap = pool_util.total_cap
    total_remaining = (
        sum(k["remaining"] for k in keys_out if k["remaining"] is not None) if total_cap is not None else None
    )
    utilization_pct = (
        round(total_used_capped / total_cap * 100, 2) if total_cap else None
    )

    return {
        "keys": keys_out,
        "aggregate": {
            "total_cap": total_cap,
            "total_used": pool_util.total_used,
            "total_remaining": total_remaining,
            "utilization_pct": utilization_pct,
            "active_keys": state_counts["active"],
            "exhausted_keys": state_counts["exhausted"],
            "cooldown_keys": state_counts["cooldown"],
            "suspended_keys": state_counts["suspended"],
            "tokens_used": total_tokens_used,
            "token_cap_total": total_token_cap or None,
            "tokens_remaining_total": total_tokens_left if total_token_cap else None,
        },
    }
