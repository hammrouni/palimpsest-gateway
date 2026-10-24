"""What a provider says about a key's rate limits.

Two sources, both operator-only and both numbers and timestamps, never key
material or provider message text:

* **Observed:** the ``*ratelimit*`` / ``retry-after`` headers that ride on every
  real provider response (OpenAI, Anthropic and OpenRouter all send some). Free:
  no extra provider call. Refreshed on each served or rate-limited request and
  kept for a day.
* **Live:** providers that have a "how much have I got left" endpoint. Today that
  is OpenRouter's ``GET /key``, called on demand from the dashboard.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.pool.models import Provider

logger = logging.getLogger("palimpsest.limits")

_KEY = "pgw:pool:ratelimit:{key_id}"
_TTL_SECONDS = 86400
_MAX_HEADERS = 24
_MAX_VALUE = 128
_PREFIX = "llm_provider-"

# What the live OpenRouter view may show. Anything else in the reply (notably
# ``label``, which carries a fragment of the key) is dropped.
_OPENROUTER_FIELDS = (
    "is_free_tier",
    "limit",
    "limit_remaining",
    "limit_reset",
    "usage",
    "usage_daily",
    "usage_weekly",
    "usage_monthly",
    "free_model_daily_requests",
)
_FREE_DAILY_FIELDS = ("used", "limit", "remaining", "reset_at")


def rate_limit_headers(headers: Mapping[str, Any] | None) -> dict[str, str]:
    """The rate-limit headers out of a provider response's headers, names lowercased.

    Accepts litellm's ``llm_provider-`` prefixed form as well as plain headers.
    """
    if not headers:
        return {}
    found: dict[str, str] = {}
    for raw_name, raw_value in headers.items():
        name = str(raw_name).lower()
        if name.startswith(_PREFIX):
            name = name[len(_PREFIX):]
        if "ratelimit" in name or name == "retry-after":
            found[name] = str(raw_value)[:_MAX_VALUE]
        if len(found) >= _MAX_HEADERS:
            break
    return found


async def record_observed(redis: Any, key_id: str, headers: Mapping[str, Any] | None) -> None:
    """Remember the latest rate-limit headers seen for a pool key. Best effort."""
    observed = rate_limit_headers(headers)
    if not observed:
        return
    key = _KEY.format(key_id=key_id)
    try:
        await redis.set(
            key,
            json.dumps({"headers": observed, "updated_at": datetime.now(UTC).isoformat()}),
            ex=_TTL_SECONDS,
        )
    except Exception:
        logger.warning("could not record rate-limit headers", exc_info=True)


async def get_observed(redis: Any, key_id: str) -> dict[str, Any] | None:
    raw = await redis.get(_KEY.format(key_id=key_id))
    if raw is None:
        return None
    try:
        return json.loads(raw.decode() if isinstance(raw, bytes) else raw)
    except ValueError:
        return None


async def get_observed_many(redis: Any, key_ids: list[str]) -> dict[str, dict[str, Any] | None]:
    """``get_observed`` for several keys in one round trip."""
    if not key_ids:
        return {}
    pipe = redis.pipeline(transaction=False)
    for key_id in key_ids:
        pipe.get(_KEY.format(key_id=key_id))
    raw_values = await pipe.execute()
    observed: dict[str, dict[str, Any] | None] = {}
    for key_id, raw in zip(key_ids, raw_values, strict=True):
        if raw is None:
            observed[key_id] = None
            continue
        try:
            observed[key_id] = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except ValueError:
            observed[key_id] = None
    return observed


# Reset values above these are epoch milliseconds, then epoch seconds. Anything
# smaller is a count of seconds from when the headers were observed.
_EPOCH_MS_FLOOR = 10**11
_EPOCH_S_FLOOR = 10**9
_ABSOLUTE_RESET_HEADERS = ("ratelimit-reset", "x-ratelimit-reset")
_RELATIVE_RESET_HEADERS = ("ratelimit-reset-after", "x-ratelimit-reset-after", "retry-after")


def _number(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None


def _reset_instant(name: str, value: str, observed_at: datetime) -> datetime | None:
    """One reset header as a UTC instant, or None if it is not a usable number."""
    number = _number(value)
    if number is None:
        return None
    try:
        if name in _ABSOLUTE_RESET_HEADERS:
            if number > _EPOCH_MS_FLOOR:
                return datetime.fromtimestamp(number / 1000, UTC)
            if number > _EPOCH_S_FLOOR:
                return datetime.fromtimestamp(number, UTC)
            return observed_at + timedelta(seconds=number)
        if name in _RELATIVE_RESET_HEADERS:
            return observed_at + timedelta(seconds=number)
    except (OverflowError, OSError, ValueError):
        return None
    return None


def quota_blocked_until(
    observed: dict[str, Any] | None, now: datetime | None = None
) -> datetime | None:
    """When a key's last observed rate limit lifts, while that limit still holds.

    The limit holds when the provider reported zero requests remaining and a
    reset time is still ahead. Without a usable reset time it does not hold:
    the next attempt on the key says more, so the key stays routable.
    """
    if not isinstance(observed, dict):
        return None
    headers = observed.get("headers")
    if not isinstance(headers, dict):
        return None
    if not any(
        "remaining" in name.lower() and _number(str(value)) is not None and _number(str(value)) <= 0
        for name, value in headers.items()
    ):
        return None
    try:
        observed_at = datetime.fromisoformat(observed["updated_at"])
    except (KeyError, TypeError, ValueError):
        return None

    resets = [
        instant
        for name, value in headers.items()
        if (instant := _reset_instant(name.lower(), str(value), observed_at)) is not None
    ]
    if not resets:
        return None
    latest = max(resets)
    return latest if latest > (now or datetime.now(UTC)) else None


async def clear_observed(redis: Any, key_id: str) -> None:
    await redis.delete(_KEY.format(key_id=key_id))


async def fetch_openrouter(api_key: str, base_url: str) -> dict[str, Any]:
    """OpenRouter's own account of this key: credits and free-model request budget.

    Raises ``LiveLimitsError`` with a message safe to show an operator.
    """
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(
                base_url.rstrip("/") + "/key", headers={"Authorization": f"Bearer {api_key}"}
            )
    except httpx.HTTPError as exc:
        raise LiveLimitsError("Could not reach OpenRouter.") from exc

    if response.status_code != 200:
        raise LiveLimitsError(f"OpenRouter answered {response.status_code} for this key.")
    try:
        body = response.json()
    except ValueError as exc:
        raise LiveLimitsError("OpenRouter returned an unreadable reply.") from exc

    data = body.get("data", body) if isinstance(body, dict) else {}
    if not isinstance(data, dict):
        data = {}
    out ={field: data[field] for field in _OPENROUTER_FIELDS if field in data}
    free = out.get("free_model_daily_requests")
    if isinstance(free, dict):
        out["free_model_daily_requests"] = {f: free[f] for f in _FREE_DAILY_FIELDS if f in free}
    return out


async def live_limits(registry: Any, key_id: str, base_url: str) -> dict[str, Any] | None:
    """The provider's live view of one pool key, or None if it has no such endpoint.

    The decrypted key is read and used here, inside the pool package, and never
    handed back: callers (the API layer included) only ever see the numbers.
    """
    record = await registry.get(key_id)
    if record is None or record.provider != Provider.OPENROUTER:
        return None
    return await fetch_openrouter(await registry.get_key_material(key_id), base_url)


class LiveLimitsError(Exception):
    """A live provider query failed; ``str()`` is operator-safe."""
