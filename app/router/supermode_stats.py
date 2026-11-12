"""Supermode statistics: what it picked, where requests went, why it moved on.

Operator-only metadata: model, provider and account ids, counts, timings. Never
message content or key material.

Layout in Redis:

* ``pgw:supermode:day:<YYYYMMDD>`` is one hash per UTC day of counters:
  ``req`` requests, ``ok`` answered, ``fail`` not answered, ``nopick`` no model
  could take the request, ``firstwin`` answered by the model first picked,
  ``hopped`` requests that moved to another model at least once, ``hops`` model
  changes, ``vision`` / ``tools`` requests that needed them, ``lat_sum`` /
  ``lat_n`` latency of answered requests, and ``served:<model>``,
  ``first:<model>``, ``provider:<provider>``, ``cause:<cause>``,
  ``acct:<account_id>`` per-name counts. The hash expires after
  ``RETENTION_DAYS``.
* ``pgw:supermode:events`` is a list of JSON events, newest first, capped.

Names come from the pool (a model is only picked if a key serves it), so the
hashes cannot be grown with arbitrary client strings.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

logger = logging.getLogger("palimpsest.supermode")

_DAY = "pgw:supermode:day:{day}"
_EVENTS = "pgw:supermode:events"
EVENTS_MAX = 300
RETENTION_DAYS = 90
_NAME_MAX = 80

# Why supermode left a model for another one.
HOP_CAUSES = ("exhausted", "context", "vision", "tools", "request")


def _name(value: object) -> str:
    return str(value or "-")[:_NAME_MAX]


async def record(redis: Any, event: dict[str, Any]) -> None:
    """Count one finished supermode request and keep it in the recent list."""
    now = datetime.now(UTC)
    day = _DAY.format(day=now.strftime("%Y%m%d"))
    served = event.get("served")
    picked = event.get("picked")
    hops = event.get("hops") or []
    needs = event.get("needs") or {}

    pipe = redis.pipeline(transaction=False)
    pipe.hincrby(day, "req", 1)
    pipe.hincrby(day, "ok" if served else "fail", 1)
    if not picked:
        pipe.hincrby(day, "nopick", 1)
    else:
        pipe.hincrby(day, "first:" + _name(picked), 1)
    if served:
        pipe.hincrby(day, "served:" + _name(served), 1)
        pipe.hincrby(day, "provider:" + _name(event.get("provider")), 1)
        if served == picked:
            pipe.hincrby(day, "firstwin", 1)
        if event.get("latency_ms") is not None:
            pipe.hincrby(day, "lat_sum", int(event["latency_ms"]))
            pipe.hincrby(day, "lat_n", 1)
    if hops:
        pipe.hincrby(day, "hopped", 1)
        pipe.hincrby(day, "hops", len(hops))
        for hop in hops:
            pipe.hincrby(day, "cause:" + _name(hop.get("cause")), 1)
    if needs.get("vision"):
        pipe.hincrby(day, "vision", 1)
    if needs.get("tools"):
        pipe.hincrby(day, "tools", 1)
    if event.get("account_id"):
        pipe.hincrby(day, "acct:" + _name(event["account_id"]), 1)
    pipe.expire(day, RETENTION_DAYS * 86400)
    pipe.lpush(_EVENTS, json.dumps({**event, "ts": now.isoformat()}, default=str))
    pipe.ltrim(_EVENTS, 0, EVENTS_MAX - 1)
    await pipe.execute()


def _decode(raw: dict) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in (raw or {}).items():
        name = key.decode() if isinstance(key, bytes) else key
        try:
            out[name] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _share(counts: dict[str, int], prefix: str) -> dict[str, int]:
    return {k[len(prefix):]: v for k, v in counts.items() if k.startswith(prefix)}


async def summary(redis: Any, days: int) -> dict[str, Any]:
    """Counters for the last ``days`` days: totals, distributions, a daily series."""
    now = datetime.now(UTC)
    moments = [now - timedelta(days=offset) for offset in range(days - 1, -1, -1)]
    pipe = redis.pipeline(transaction=False)
    for moment in moments:
        pipe.hgetall(_DAY.format(day=moment.strftime("%Y%m%d")))
    rows = [_decode(row) for row in await pipe.execute()]

    total: dict[str, int] = {}
    daily = []
    for moment, row in zip(moments, rows, strict=True):
        for key, value in row.items():
            total[key] = total.get(key, 0) + value
        daily.append(
            {
                "day": moment.date().isoformat(),
                "requests": row.get("req", 0),
                "ok": row.get("ok", 0),
                "failed": row.get("fail", 0),
                "hopped": row.get("hopped", 0),
            }
        )

    requests = total.get("req", 0)
    lat_n = total.get("lat_n", 0)
    served = _share(total, "served:")
    first = _share(total, "first:")
    models = [
        {"model": model, "served": served.get(model, 0), "first_pick": first.get(model, 0)}
        for model in sorted(set(served) | set(first))
    ]
    models.sort(key=lambda row: (-row["served"], -row["first_pick"], row["model"]))
    return {
        "days": days,
        "totals": {
            "requests": requests,
            "ok": total.get("ok", 0),
            "failed": total.get("fail", 0),
            "no_model": total.get("nopick", 0),
            "first_pick_served": total.get("firstwin", 0),
            "hopped": total.get("hopped", 0),
            "hops": total.get("hops", 0),
            "needed_vision": total.get("vision", 0),
            "needed_tools": total.get("tools", 0),
            "avg_latency_ms": round(total.get("lat_sum", 0) / lat_n) if lat_n else None,
        },
        "daily": daily,
        "models": models,
        "providers": sorted(
            ({"provider": p, "served": n} for p, n in _share(total, "provider:").items()),
            key=lambda row: (-row["served"], row["provider"]),
        ),
        "accounts": sorted(
            ({"account_id": a, "requests": n} for a, n in _share(total, "acct:").items()),
            key=lambda row: (-row["requests"], row["account_id"]),
        )[:50],
        "causes": {cause: _share(total, "cause:").get(cause, 0) for cause in HOP_CAUSES},
    }


async def recent(redis: Any, limit: int = 50) -> list[dict[str, Any]]:
    """The newest events, newest first."""
    raw = await redis.lrange(_EVENTS, 0, max(0, limit - 1))
    events = []
    for item in raw:
        try:
            events.append(json.loads(item.decode() if isinstance(item, bytes) else item))
        except ValueError:
            continue
    return events
