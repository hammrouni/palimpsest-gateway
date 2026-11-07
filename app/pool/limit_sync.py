"""Check a key's limits against what the provider says, and fix the local copy.

Only OpenRouter has a "how much have I got left" endpoint (``GET /key``), so
only OpenRouter keys can be synced. The provider is the source of truth for two
local numbers:

* ``daily_cap``: OpenRouter's free-model request limit (50/day on a free
  account, 1000/day once it holds credits).
* the ``used`` counter: how many of those requests the account already spent,
  including requests made outside this gateway.

Report-only mode (``apply=False``) shows the drift without writing anything.
"""
from __future__ import annotations

import logging
from typing import Any

from app.pool.limits import LiveLimitsError, fetch_openrouter
from app.pool.models import KeyUpdate, Provider
from app.pool.registry import KeyNotFoundError, PoolRegistry
from app.settlement.read import _used
from app.settlement.reconcile import set_used

logger = logging.getLogger("palimpsest.limit_sync")

# OpenRouter's documented free-model daily limits, for when /key omits the block.
_FREE_TIER_LIMIT = 50
_PAID_TIER_LIMIT = 1000


def _count(value: Any) -> int | None:
    """A non-negative whole number out of a provider field, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if value != value or value < 0:  # NaN or negative
        return None
    return int(value)


def provider_view(live: dict[str, Any]) -> dict[str, Any]:
    """The daily request budget OpenRouter reports: ``limit``, ``used``, ``reset_at``."""
    free = live.get("free_model_daily_requests")
    free = free if isinstance(free, dict) else {}

    limit = _count(free.get("limit"))
    if limit is None:
        tier = live.get("is_free_tier")
        if tier is True:
            limit = _FREE_TIER_LIMIT
        elif tier is False:
            limit = _PAID_TIER_LIMIT

    used = _count(free.get("used"))
    if used is None and limit is not None:
        remaining = _count(free.get("remaining"))
        if remaining is not None:
            used = max(0, limit - remaining)

    return {
        "limit": limit,
        "used": used,
        "reset_at": free.get("reset_at"),
        "is_free_tier": live.get("is_free_tier"),
    }


async def sync_key(
    registry: PoolRegistry,
    key_id: str,
    base_url: str,
    *,
    apply: bool = True,
    operator_id: str | None = None,
) -> dict[str, Any]:
    """Compare one key with its provider and, if ``apply``, correct the local side.

    ``status``: ``ok`` (already matches), ``updated`` (local fixed), ``drift``
    (report-only, differs), ``unsupported`` (provider has no limits endpoint),
    ``error`` (the provider could not be asked; nothing changed).
    Raises ``KeyNotFoundError`` for an unknown key.
    """
    record = await registry.get(key_id)
    if record is None:
        raise KeyNotFoundError(key_id)

    result: dict[str, Any] = {
        "key_id": key_id,
        "label": record.label,
        "provider": record.provider.value,
        "status": "unsupported",
        "applied": False,
        "changes": [],
        "provider_limits": None,
        "local": None,
        "error": None,
    }
    if record.provider != Provider.OPENROUTER:
        return result

    local_used = await _used(registry.redis, key_id, record)
    result["local"] = {"daily_cap": record.daily_cap, "used": local_used}

    try:
        live = await fetch_openrouter(await registry.get_key_material(key_id), base_url)
    except LiveLimitsError as exc:
        result["status"] = "error"
        result["error"] = str(exc)
        return result

    view = provider_view(live)
    result["provider_limits"] = view

    changes: list[dict[str, Any]] = []
    if view["limit"] is not None and record.daily_cap != view["limit"]:
        changes.append({"field": "daily_cap", "local": record.daily_cap, "provider": view["limit"]})
    if view["used"] is not None and local_used != view["used"]:
        changes.append({"field": "used", "local": local_used, "provider": view["used"]})
    result["changes"] = changes

    if not changes:
        result["status"] = "ok"
        return result
    if not apply:
        result["status"] = "drift"
        return result

    for change in changes:
        if change["field"] == "daily_cap":
            await registry.update(
                key_id, KeyUpdate(daily_cap=float(change["provider"])), operator_id=operator_id
            )
        else:
            await set_used(registry.redis, record, int(change["provider"]))
    logger.info("synced limits for %s: %s", key_id, [c["field"] for c in changes])
    result["status"] = "updated"
    result["applied"] = True
    return result


async def sync_all(
    registry: PoolRegistry,
    base_url: str,
    *,
    apply: bool = True,
    operator_id: str | None = None,
) -> list[dict[str, Any]]:
    """``sync_key`` for every OpenRouter key; one failing key does not stop the rest."""
    results: list[dict[str, Any]] = []
    for record in await registry.list():
        if record.provider != Provider.OPENROUTER:
            continue
        try:
            results.append(
                await sync_key(registry, record.id, base_url, apply=apply, operator_id=operator_id)
            )
        except KeyNotFoundError:
            continue  # removed mid-run
    return results
