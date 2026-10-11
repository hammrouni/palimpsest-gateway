"""Quota-aware key selection: which pool key serves this request.

Read-only: routable keys from the registry plus one pipelined read of the
used counters. Key state is changed by the failover loop, counters by
settlement.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import UTC, datetime

from app.pool.models import KeyRecord, Provider, window_bounds
from app.pool.registry import PoolRegistry

_USED_COUNTER_PREFIX = "pgw:pool:key"


@dataclass(frozen=True)
class Candidate:
    """One selectable key and its remaining daily cap (``None`` = uncapped)."""

    key_id: str
    provider: Provider
    remaining_cap: int | None


class NoCandidateError(Exception):
    """No candidate serves ``model``. Carries no provider or key identity."""

    def __init__(self, model: str):
        self.model = model
        super().__init__(f"No active capacity available for model {model!r}.")


def _used_counter_key(key_id: str) -> str:
    return f"{_USED_COUNTER_PREFIX}:{key_id}:used"


def _used_reset_at_key(key_id: str) -> str:
    return f"{_USED_COUNTER_PREFIX}:{key_id}:used_reset_at"


async def _remaining_by_id(
    registry: PoolRegistry, records: list[KeyRecord]
) -> dict[str, int | None]:
    """Remaining cap per key, from one pipelined read of the used counters.

    A counter stamped before the key's current window counts as 0; it is only
    reset by the next settlement.
    """
    if not records:
        return {}

    pipe = registry.redis.pipeline(transaction=False)
    for record in records:
        pipe.get(_used_counter_key(record.id))
        pipe.get(_used_reset_at_key(record.id))
    raw = await pipe.execute()

    now = datetime.now(UTC)
    remaining: dict[str, int | None] = {}
    for i, record in enumerate(records):
        cap = record.daily_cap
        if cap is None:
            remaining[record.id] = None
            continue
        used_raw, stamped_raw = raw[2 * i], raw[2 * i + 1]
        try:
            used = max(0, int(used_raw or 0))
        except (ValueError, TypeError):
            used = 0
        try:
            stamped = float(stamped_raw) if stamped_raw is not None else None
        except (ValueError, TypeError):
            stamped = None
        if used and stamped is not None:
            window_start, _ = window_bounds(record.reset_at, record.reset_period_hours, now)
            if stamped < window_start:
                used = 0
        remaining[record.id] = max(0, math.floor(cap) - used)
    return remaining


async def select_candidates(
    registry: PoolRegistry,
    model: str,
    excluded: frozenset[str] = frozenset(),
    preferred_provider: Provider | None = None,
) -> list[Candidate]:
    """The deterministic, ordered candidate list (never raises).

    Routable keys for ``model`` minus ``excluded``, without keys whose cap is
    used up, ordered: the preferred provider's capped keys, then other capped
    keys (both by remaining desc, then key_id), then uncapped keys (preferred
    provider first, then key_id).
    """
    routable = await registry.routable_keys(model=model)
    universe = [record for record in routable if record.id not in excluded]

    remaining_by_id = await _remaining_by_id(registry, universe)

    capped: list[Candidate] = []
    uncapped: list[Candidate] = []
    for record in universe:
        remaining = remaining_by_id[record.id]
        if remaining == 0:
            continue
        candidate = Candidate(
            key_id=record.id, provider=record.provider, remaining_cap=remaining
        )
        (uncapped if remaining is None else capped).append(candidate)

    def _capped_key(c: Candidate) -> tuple:
        return (-c.remaining_cap, c.key_id)

    if preferred_provider is not None:
        tier1 = sorted(
            (c for c in capped if c.provider == preferred_provider), key=_capped_key
        )
        tier2 = sorted(
            (c for c in capped if c.provider != preferred_provider), key=_capped_key
        )
    else:
        tier1 = []
        tier2 = sorted(capped, key=_capped_key)

    def _uncapped_key(c: Candidate) -> tuple:
        preferred_first = 0 if c.provider == preferred_provider else 1
        return (preferred_first, c.key_id)

    tier3 = sorted(uncapped, key=_uncapped_key)

    return [*tier1, *tier2, *tier3]


async def select(
    registry: PoolRegistry,
    model: str,
    excluded: frozenset[str] = frozenset(),
    preferred_provider: Provider | None = None,
) -> Candidate | None:
    """The first candidate, or ``None`` when there is none (never raises)."""
    candidates = await select_candidates(
        registry, model, excluded=excluded, preferred_provider=preferred_provider
    )
    return candidates[0] if candidates else None
