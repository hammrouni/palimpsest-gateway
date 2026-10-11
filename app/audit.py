"""Operator audit trail: who did what, to what, and when.

A bounded newest-first list. Best-effort: a write failure is logged and never
changes the outcome of the action being audited. Never records key material.
"""
from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger("palimpsest.audit")

_KEY = "pgw:audit"
_MAX = 5000


async def audit(
    redis: Any,
    operator_id: str,
    action: str,
    target: str | None = None,
    detail: dict[str, Any] | None = None,
) -> None:
    """Append one audit record. Never raises."""
    record = {
        "ts": datetime.now(UTC).isoformat(),
        "operator_id": operator_id,
        "action": action,
        "target": target,
        "detail": detail or {},
    }
    try:
        async with redis.pipeline(transaction=False) as pipe:
            pipe.lpush(_KEY, json.dumps(record, default=str))
            pipe.ltrim(_KEY, 0, _MAX - 1)
            await pipe.execute()
    except Exception:
        logger.error("audit write failed for %s", action, exc_info=True)


async def recent_audit(redis: Any, limit: int, action: str | None = None) -> list[dict[str, Any]]:
    """Newest first; ``action`` filters by prefix (``account`` matches ``account.disable``)."""
    raw = await redis.lrange(_KEY, 0, _MAX - 1)
    out: list[dict[str, Any]] = []
    for item in raw:
        try:
            record = json.loads(item.decode() if isinstance(item, bytes) else item)
        except ValueError:
            continue
        if action and not str(record.get("action", "")).startswith(action):
            continue
        out.append(record)
        if len(out) >= limit:
            break
    return out
