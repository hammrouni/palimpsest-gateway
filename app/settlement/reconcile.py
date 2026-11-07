"""Overwrite a key's used counter with the provider's own figure.

Used-counter writes live only in ``app/settlement``; this is the one sanctioned
write outside ``settle()``, used by the provider limit sync.
"""
from __future__ import annotations

from datetime import UTC, datetime

from redis.asyncio import Redis

from app.pool.models import KeyRecord, window_bounds
from app.settlement.settle import _used_key, _used_reset_at_key


async def set_used(redis: Redis, record: KeyRecord, used: int) -> None:
    """Set the key's used counter for its current quota window."""
    now = datetime.now(UTC)
    window_start, next_reset = window_bounds(record.reset_at, record.reset_period_hours, now)
    ttl = max(1, int(next_reset - now.timestamp()) + 60)
    pipe = redis.pipeline(transaction=True)
    pipe.set(_used_key(record.id), used, ex=ttl)
    pipe.set(_used_reset_at_key(record.id), window_start, ex=ttl)
    await pipe.execute()
