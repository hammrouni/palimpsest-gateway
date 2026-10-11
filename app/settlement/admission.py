"""Admission gate: the per-account daily cap, then the pool low-water mark.

The per-account cap has its own counter (``pgw:user:{a}:used``); the
low-water mark is a read-only decision over the per-key remaining capacity.
"""
from __future__ import annotations

import json
import logging
import math
import pathlib
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, time

from redis.asyncio import Redis

from app.config import get_settings
from app.settlement.read import utilization
from app.settlement.settle import _now

_LUA_PATH = pathlib.Path(__file__).parent / "admission.lua"
_LUA_SRC = _LUA_PATH.read_text(encoding="utf-8")

logger = logging.getLogger("palimpsest.settlement.admission")

USER_QUOTA_EXHAUSTED = "user_quota_exhausted"
POOL_LOW_WATER = "pool_low_water"


def _user_used_key(account_id: str) -> str:
    return f"pgw:user:{account_id}:used"


def _user_used_reset_at_key(account_id: str) -> str:
    return f"pgw:user:{account_id}:used_reset_at"


def _user_cap_key(account_id: str) -> str:
    return f"pgw:user:{account_id}:cap"


def _daily_utc_window_bounds(now: datetime) -> tuple[float, float]:
    """The current account window: one UTC day, shared by every account."""
    midnight = datetime.combine(now.date(), time.min, tzinfo=UTC)
    window_start = midnight.timestamp()
    return window_start, window_start + 86400


@dataclass(frozen=True)
class AdmissionResult:
    """Outcome of one ``admit()`` call."""

    admitted: bool
    code: str | None
    message: str | None
    next_reset: datetime | None = None


async def _peek_user_state(
    account_id: str, redis: Redis, now: datetime
) -> tuple[int, int | None, float, float]:
    """Read-only: the account's ``(used, cap, window_start, next_reset)``."""
    window_start, next_reset = _daily_utc_window_bounds(now)

    pipe = redis.pipeline(transaction=False)
    pipe.get(_user_used_key(account_id))
    pipe.get(_user_used_reset_at_key(account_id))
    pipe.get(_user_cap_key(account_id))
    raw_used, raw_window_start, raw_cap = await pipe.execute()

    used = 0
    try:
        used = int(raw_used) if raw_used else 0
    except (TypeError, ValueError):
        logger.warning(
            json.dumps({"event": "user_counter_corrupt", "account_id": account_id})
        )
    stored_window_start = window_start
    try:
        stored_window_start = float(raw_window_start) if raw_window_start else window_start
    except (TypeError, ValueError):
        logger.warning(
            json.dumps({"event": "user_reset_at_corrupt", "account_id": account_id})
        )
    if stored_window_start < window_start:  # crossed a reset boundary
        used = 0

    cap = get_settings().user_daily_cap
    try:
        cap = int(raw_cap) if raw_cap is not None else get_settings().user_daily_cap
    except (TypeError, ValueError):
        logger.warning(
            json.dumps({"event": "user_cap_corrupt", "account_id": account_id})
        )
    return used, cap, window_start, next_reset


async def _aggregate_remaining(redis: Redis) -> int:
    """Sum of remaining capacity over every capped key."""
    result = await utilization(redis=redis)
    capped = [k.remaining for k in result.per_key if k.remaining is not None]
    if not capped:
        # No capped key: nothing finite to run low on.
        return sys.maxsize
    return sum(capped)


async def admit(account_id: str, *, redis: Redis) -> AdmissionResult:
    """Per-account cap, then the pool low-water mark.

    Increments the account counter only when both pass; a rejection writes
    nothing.
    """
    now = _now()
    used, cap, window_start, next_reset = await _peek_user_state(account_id, redis, now)

    if cap is not None and used >= cap:
        return AdmissionResult(
            admitted=False,
            code=USER_QUOTA_EXHAUSTED,
            message=(
                f"Daily quota of {cap} requests exceeded. "
                f"Resets at {datetime.fromtimestamp(next_reset, tz=UTC).isoformat()}."
            ),
            next_reset=datetime.fromtimestamp(next_reset, tz=UTC),
        )

    water_mark = get_settings().pool_low_water_mark
    if water_mark > 0:
        # Not atomic with the increment below: a concurrent settlement can let
        # one extra request through. Accepted; this is a fairness throttle and
        # settlement's per-key guard still prevents going over a key's cap.
        aggregate_remaining = await _aggregate_remaining(redis)
        if aggregate_remaining <= water_mark:
            return AdmissionResult(
                admitted=False,
                code=POOL_LOW_WATER,
                message="Pool capacity is low; request throttled for fairness. Try again shortly.",
            )

    script = redis.register_script(_LUA_SRC)
    admitted, _new_used = await script(
        keys=[_user_used_key(account_id), _user_used_reset_at_key(account_id)],
        args=[now.timestamp(), window_start, next_reset, -1 if cap is None else cap],
    )

    if admitted == 1:
        return AdmissionResult(admitted=True, code=None, message=None)

    # Lost a race for the last unit; nothing was written.
    return AdmissionResult(
        admitted=False,
        code=USER_QUOTA_EXHAUSTED,
        message=(
            f"Daily quota of {cap} requests exceeded. "
            f"Resets at {datetime.fromtimestamp(next_reset, tz=UTC).isoformat()}."
        ),
        next_reset=datetime.fromtimestamp(next_reset, tz=UTC),
    )


async def raise_user_cap(account_id: str, new_cap: int, *, redis: Redis) -> bool:
    """Set an account's cap for the rest of today (UTC); False at the low-water mark.

    The override expires just after the next UTC midnight.
    """
    water_mark = get_settings().pool_low_water_mark
    if water_mark > 0:
        aggregate_remaining = await _aggregate_remaining(redis)
        if aggregate_remaining <= water_mark:
            return False

    if isinstance(new_cap, bool) or not isinstance(new_cap, int) or new_cap < 1:
        raise ValueError("new_cap must be a positive integer")

    now = _now()
    _window_start, next_reset = _daily_utc_window_bounds(now)
    ttl = max(1, math.floor(next_reset - now.timestamp()) + 60)
    await redis.set(_user_cap_key(account_id), new_cap, ex=ttl)
    return True


async def forget_account(account_id: str, *, redis: Redis) -> None:
    """Drop an account's daily usage counter, window marker and cap override."""
    await redis.delete(
        _user_used_key(account_id),
        _user_used_reset_at_key(account_id),
        _user_cap_key(account_id),
    )


# Give back the unit admit() took, only within the same daily window (a refund
# after midnight must not eat into the new day) and never below zero.
_REFUND_LUA = """
local used = tonumber(redis.call('GET', KEYS[1]))
local ws = tonumber(redis.call('GET', KEYS[2]))
if used == nil or ws == nil or used <= 0 or ws ~= tonumber(ARGV[1]) then
  return 0
end
redis.call('DECR', KEYS[1])
return 1
"""


async def refund(account_id: str, *, redis: Redis) -> bool:
    """Give one admitted request back to the account's daily allowance.

    For requests no provider served. Never raises; True when refunded.
    """
    try:
        window_start, _next_reset = _daily_utc_window_bounds(_now())
        refunded = await redis.eval(
            _REFUND_LUA,
            2,
            _user_used_key(account_id),
            _user_used_reset_at_key(account_id),
            window_start,
        )
        return int(refunded) == 1
    except Exception:
        logger.error(json.dumps({"event": "user_refund_failed", "account_id": account_id}))
        return False


async def override_daily_cap(account_id: str, new_cap: int, *, redis: Redis) -> datetime | None:
    """Set ``account_id``'s cap for the current daily window.

    Args:
        account_id: Account to override.
        new_cap: Requests allowed today (>= 1).
        redis: Shared Redis client.

    Returns:
        When the override lapses (the next UTC midnight), or None when refused
        because the pool is at or below its low-water mark.
    """
    if not await raise_user_cap(account_id, new_cap, redis=redis):
        return None
    _window_start, next_reset = _daily_utc_window_bounds(_now())
    return datetime.fromtimestamp(next_reset, tz=UTC)
