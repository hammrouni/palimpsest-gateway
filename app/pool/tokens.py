"""Per-key token consumption inside the key's current reset window.

Counts the provider-reported tokens (prompt plus completion) of requests a key
served. The counter lives under a name that includes the window it belongs to, so
when the window rolls over a new counter starts at zero by itself: "refreshed when
the key's tokens come back" needs no job and no reset step. Old counters expire a
day after their window ends.

Indication only: nothing here routes around or blocks a key. A request whose
provider reported no token counts (some streams) adds nothing, so the figure is a
floor, not a promise.
"""
from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from app.pool.models import KeyRecord, window_bounds

logger = logging.getLogger("palimpsest.tokens")

_KEEP_AFTER_WINDOW_SECONDS = 86400


def _counter_key(key_id: str, window_start: float) -> str:
    return f"pgw:pool:key:{key_id}:tokens:{int(window_start)}"


def counter_pattern(key_id: str) -> str:
    return f"pgw:pool:key:{key_id}:tokens:*"


async def add_tokens(redis: Any, record: KeyRecord, tokens: int, now: datetime | None = None) -> None:
    """Add one served request's tokens to the key's current window."""
    if tokens <= 0:
        return
    now = now or datetime.now(UTC)
    start, next_reset = window_bounds(record.reset_at, record.reset_period_hours, now)
    key = _counter_key(record.id, start)
    async with redis.pipeline(transaction=False) as pipe:
        pipe.incrby(key, int(tokens))
        pipe.expireat(key, int(next_reset) + _KEEP_AFTER_WINDOW_SECONDS)
        await pipe.execute()


async def carry_over(
    redis: Any, before: KeyRecord, after: KeyRecord, now: datetime | None = None
) -> None:
    """Keep a key's tokens when its reset schedule is edited.

    The counter is named after the window it belongs to, so a new schedule would
    otherwise leave what was already used stranded under the old name and the key
    would read as untouched. The usage so far moves into the new current window.
    """
    now = now or datetime.now(UTC)
    old_start, _ = window_bounds(before.reset_at, before.reset_period_hours, now)
    new_start, new_end = window_bounds(after.reset_at, after.reset_period_hours, now)
    if int(old_start) == int(new_start):
        return
    old_key = _counter_key(before.id, old_start)
    raw = await redis.get(old_key)
    if raw is None:
        return
    new_key = _counter_key(after.id, new_start)
    async with redis.pipeline(transaction=True) as pipe:
        pipe.incrby(new_key, int(raw))
        pipe.expireat(new_key, int(new_end) + _KEEP_AFTER_WINDOW_SECONDS)
        pipe.delete(old_key)
        await pipe.execute()


async def tokens_used(redis: Any, record: KeyRecord, now: datetime | None = None) -> int:
    """Tokens this key has served in its current window; 0 once a new window begins."""
    now = now or datetime.now(UTC)
    start, _next = window_bounds(record.reset_at, record.reset_period_hours, now)
    raw = await redis.get(_counter_key(record.id, start))
    try:
        return int(raw) if raw is not None else 0
    except (TypeError, ValueError):
        logger.warning("corrupt token counter for key %s", record.id)
        return 0
