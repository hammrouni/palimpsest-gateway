"""A bounded, best-effort list of failover records for the operator.

Written by settlement for requests with two or more attempts. A write failure
is logged and swallowed; it never affects the client or the settlement.
"""
from __future__ import annotations

import json
import logging
import time
from datetime import UTC, datetime

from redis.asyncio import Redis

logger = logging.getLogger("palimpsest.pool.events")

_EVENTS_KEY = "pgw:pool:failover_events"

_last_ts_ns = 0


def _monotonic_ts() -> str:
    """A strictly increasing, sortable timestamp string (per process).

    Equal timestamps would drop events at page boundaries, since the cursor
    is "strictly older than". Coarse clocks make ties likely without this.
    """
    global _last_ts_ns
    now_ns = time.time_ns()
    if now_ns <= _last_ts_ns:
        now_ns = _last_ts_ns + 1
    _last_ts_ns = now_ns

    seconds, nanos = divmod(now_ns, 1_000_000_000)
    dt = datetime.fromtimestamp(seconds, tz=UTC)
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{nanos:09d}+00:00"


def build_failover_event(
    *,
    request_id: str,
    account_id: str,
    session_id: str | None,
    model: str,
    chain: list[str],
    status_code: int,
    latency_ms: int,
    terminal: str,
) -> dict:
    """One failover record from a settled request; ``outcome`` is operator-facing."""
    if terminal == "success":
        outcome = "success"
    elif terminal in ("budget_exhausted", "pool_exhausted"):
        outcome = "exhausted_503"
    else:
        outcome = "error_5xx" if status_code >= 500 else "error_4xx"

    return {
        "ts": _monotonic_ts(),
        "request_id": request_id,
        "account": account_id,
        "session": session_id,
        "model": model,
        "source_key_id": chain[0] if chain else None,
        "target_key_id": chain[-1] if chain else None,
        "chain": chain,
        "attempts": len(chain),
        "duration_ms": latency_ms,
        "outcome": outcome,
        "status": status_code,
    }


async def append_failover_event(record: dict, *, redis: Redis, max_size: int) -> None:
    """Push one event onto the newest-first, bounded list. Never raises."""
    try:
        pipe = redis.pipeline(transaction=True)
        pipe.lpush(_EVENTS_KEY, json.dumps(record))
        pipe.ltrim(_EVENTS_KEY, 0, max_size - 1)
        await pipe.execute()
    except Exception:
        logger.error(
            json.dumps(
                {"event": "failover_event_append_failed", "request_id": record.get("request_id")}
            )
        )


_SCAN_CHUNK = 200


async def recent_failover_events(
    limit: int, before: str | None, *, redis: Redis
) -> tuple[list[dict], str | None]:
    """Newest-first page, paginated on the ``ts`` field.

    ``before`` (a previous page's ``next_before``) returns only older events.
    ``next_before`` is set when the page is full, else ``None``. The list is
    read in small slices and the scan stops once the page is full.
    """
    page: list[dict] = []
    start = 0
    while len(page) < limit:
        raw_items = await redis.lrange(_EVENTS_KEY, start, start + _SCAN_CHUNK - 1)
        if not raw_items:
            break
        start += len(raw_items)
        for item in raw_items:
            try:
                event = json.loads(item)
            except (json.JSONDecodeError, TypeError):
                logger.warning(
                    json.dumps({"event": "failover_event_parse_skipped", "item": item})
                )
                continue
            if before is not None and not event.get("ts", "") < before:
                continue
            page.append(event)
            if len(page) == limit:
                break
    next_before = page[-1]["ts"] if len(page) == limit else None
    return page, next_before
