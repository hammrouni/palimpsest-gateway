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
from datetime import UTC, datetime
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
