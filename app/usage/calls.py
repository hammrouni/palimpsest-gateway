"""Call history: one compact record per finished request.

Operator-only metadata: account, key, provider and model ids, status codes,
token counts and timings, plus every key and model the request tried in order.
Never message content or key material.

``usage:calls`` is a list of JSON records, newest first, trimmed to
``usage_recent_max``. It is written for every request, whether or not the Live
Flow page is open, so the page can show a long history and, from the chain of
attempts, which key served which model.
"""
from __future__ import annotations

import json
import logging
from typing import Any

from app.config import get_settings

logger = logging.getLogger("palimpsest.calls")

_CALLS = "usage:calls"


async def record(redis: Any, call: dict[str, Any]) -> None:
    """Add one finished request to the history."""
    keep = get_settings().usage_recent_max
    pipe = redis.pipeline(transaction=False)
    pipe.lpush(_CALLS, json.dumps(call, default=str))
    pipe.ltrim(_CALLS, 0, keep - 1)
    await pipe.execute()


async def recent(redis: Any, limit: int, account_id: str | None = None) -> list[dict[str, Any]]:
    """The newest ``limit`` calls, newest first, optionally one account's."""
    raw = await redis.lrange(_CALLS, 0, get_settings().usage_recent_max - 1)
    calls: list[dict[str, Any]] = []
    for item in raw:
        try:
            call = json.loads(item.decode() if isinstance(item, bytes) else item)
        except ValueError:
            continue
        if account_id and call.get("account_id") != account_id:
            continue
        calls.append(call)
        if len(calls) >= limit:
            break
    return calls
