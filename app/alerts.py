"""Operator alert rules, their evaluation and webhook delivery.

Rules are evaluated against live state on demand (the dashboard bell) and on a
timer (webhook). A firing alert is delivered once per ``RESEND_SECONDS``.
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field, field_validator

from app.config import get_settings
from app.settlement import utilization_view
from app.usage.stats import StatsStore

logger = logging.getLogger("palimpsest.alerts")

_KEY = "pgw:operator:alerts"
_SENT = "pgw:alerts:sent:{id}"
RESEND_SECONDS = 3600
EVAL_INTERVAL_SECONDS = 60


class AlertRules(BaseModel):
    enabled: bool = True
    webhook_url: str | None = Field(default=None, max_length=2048)
    pool_remaining_pct: float | None = Field(default=20, ge=0, le=100)
    error_rate_pct: float | None = Field(default=25, ge=0, le=100)
    error_rate_min_requests: int = Field(default=20, ge=1, le=100000)
    user_cap_pct: float | None = Field(default=90, ge=1, le=100)
    key_problems: bool = True

    @field_validator("webhook_url")
    @classmethod
    def _http_only(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        parsed = urlparse(value.strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            raise ValueError("webhook_url must be an http(s) URL")
        return value.strip()


class AlertStore:
    def __init__(self, redis: Any) -> None:
        self.redis = redis

    async def get(self) -> AlertRules:
        raw = await self.redis.get(_KEY)
        if not raw:
            return AlertRules()
        try:
            return AlertRules.model_validate_json(raw)
        except ValueError:
            return AlertRules()

    async def set(self, rules: AlertRules) -> AlertRules:
        await self.redis.set(_KEY, json.dumps(rules.model_dump()))
        return rules


def default_user_cap() -> int | None:
    """The configured per-user daily request cap, for display and alerting only."""
    return get_settings().user_daily_cap


async def evaluate(redis: Any, rules: AlertRules | None = None) -> list[dict[str, Any]]:
    """Alerts firing right now, most severe first."""
    rules = rules or await AlertStore(redis).get()
    alerts: list[dict[str, Any]] = []

    def add(alert_id: str, severity: str, title: str, detail: str) -> None:
        alerts.append({"id": alert_id, "severity": severity, "title": title, "detail": detail})

    util = await utilization_view(redis=redis)
    aggregate = util.get("aggregate") or {}
    if rules.key_problems:
        for key in util.get("keys", []):
            name = key.get("label") or key.get("key_hint") or key["key_id"]
            if key["state"] == "suspended":
                add(f"key-suspended:{key['key_id']}", "warning", "Key suspended", f"{name} is suspended.")
            elif key["state"] == "exhausted":
                add(f"key-exhausted:{key['key_id']}", "critical", "Key exhausted", f"{name} is out of quota.")
        if util.get("keys") and aggregate.get("active_keys", 0) == 0:
            add("pool-no-active", "critical", "No active keys", "Every key is paused, exhausted or suspended.")
    if rules.pool_remaining_pct is not None:
        used = aggregate.get("utilization_pct")
        if used is not None and 100 - used <= rules.pool_remaining_pct:
            add(
                "pool-low",
                "critical" if 100 - used <= rules.pool_remaining_pct / 2 else "warning",
                "Pool capacity low",
                f"{100 - used:.1f}% of capped pool capacity left (alert at {rules.pool_remaining_pct:g}%).",
            )

    stats = StatsStore(redis)
    hour = (await stats.hourly(1))["hourly"][-1]
    if rules.error_rate_pct is not None and hour["requests"] >= rules.error_rate_min_requests:
        rate = (hour["error_rate"] or 0) * 100
        if rate >= rules.error_rate_pct:
            add(
                "error-rate",
                "critical",
                "High error rate",
                f"{rate:.1f}% of {hour['requests']} requests this hour failed (alert at {rules.error_rate_pct:g}%).",
            )

    cap = default_user_cap()
    if rules.user_cap_pct is not None and cap:
        daily = await stats.daily(1)
        for account_id, summary in daily["accounts"].items():
            pct = summary["requests"] / cap * 100
            if pct >= rules.user_cap_pct:
                add(
                    f"user-cap:{account_id}",
                    "warning",
                    "User near daily cap",
                    f"{account_id} used {summary['requests']} of {cap} requests today ({pct:.0f}%).",
                )

    order = {"critical": 0, "warning": 1}
    alerts.sort(key=lambda a: order.get(a["severity"], 2))
    return alerts


async def deliver(redis: Any, alerts: list[dict[str, Any]], rules: AlertRules) -> int:
    """POST newly-firing alerts to the webhook; returns how many were sent."""
    if not rules.enabled or not rules.webhook_url or not alerts:
        return 0
    fresh = []
    for alert in alerts:
        if await redis.set(_SENT.format(id=alert["id"]), "1", nx=True, ex=RESEND_SECONDS):
            fresh.append(alert)
    if not fresh:
        return 0
    payload = {
        "source": "palimpsest-gateway",
        "sent_at": datetime.now(UTC).isoformat(),
        "alerts": fresh,
        "text": "\n".join(f"[{a['severity']}] {a['title']}: {a['detail']}" for a in fresh),
    }
    async with httpx.AsyncClient(timeout=10) as client:
        response = await client.post(rules.webhook_url, json=payload)
        response.raise_for_status()
    return len(fresh)


async def alert_loop(redis: Any) -> None:
    """Evaluate and deliver forever; errors are logged, never fatal."""
    while True:
        await asyncio.sleep(EVAL_INTERVAL_SECONDS)
        try:
            rules = await AlertStore(redis).get()
            if rules.enabled and rules.webhook_url:
                await deliver(redis, await evaluate(redis, rules), rules)
        except Exception:
            logger.warning("alert evaluation failed", exc_info=True)
