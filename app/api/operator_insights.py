"""Operator analytics: overview, performance, costs, alerts, audit, export, system.

Metadata only, like the rest of the operator realm: ids, counts, timings and
operator-set prices. Never message content or key material.
"""
from __future__ import annotations

import csv
import io
import time
from datetime import UTC, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import PlainTextResponse, Response

from app.alerts import AlertRules, AlertStore, default_user_cap, deliver, evaluate
from app.api.deps import get_store
from app.api.operator_deps import get_current_operator
from app.audit import audit, recent_audit
from app.config import get_settings
from app.errors import GatewayError
from app.session.store import SessionStore
from app.settlement import utilization_view
from app.store.redis_store import RedisStore
from app.usage.pricing import Pricing, PricingStore, cost_of
from app.usage.stats import StatsStore, percentile
from app.usage.store import UsageStore

router = APIRouter(prefix="/operator/v1", tags=["operator"])

Store = Annotated[RedisStore, Depends(get_store)]
Operator = Annotated[dict, Depends(get_current_operator)]


def _sum_days(days: list[dict[str, Any]]) -> dict[str, int]:
    keys = ("requests", "ok", "errors_4xx", "errors_5xx", "exhausted", "failovers",
            "prompt_tokens", "completion_tokens")
    return {k: sum(d.get(k, 0) for d in days) for k in keys}


def _priced(pricing: Pricing, rows: list[dict[str, Any]]) -> tuple[float, int]:
    """Total cost of rows, and the tokens whose model has no price."""
    total = 0.0
    unpriced = 0
    for row in rows:
        cost = cost_of(pricing, row["model"], row["prompt_tokens"], row["completion_tokens"])
        if cost is None:
            unpriced += row["prompt_tokens"] + row["completion_tokens"]
        else:
            total += cost
    return total, unpriced


@router.get("/overview")
async def overview(
    store: Store,
    operator: Operator,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> dict:
    """Headline numbers for the last ``days`` days against the ``days`` before."""
    redis = store.redis
    stats = StatsStore(redis)
    current = await stats.daily(days)
    both = await stats.daily(days * 2)
    previous = _sum_days(both["daily"][: days])  # oldest ``days`` days of the doubled window
    util = await utilization_view(redis=redis)
    pricing = await PricingStore(redis).get()
    usage = await UsageStore(redis).usage(days)
    cost, unpriced = _priced(pricing, usage["rows"])
    prev_usage = await UsageStore(redis).usage(days * 2)
    prev_cost, _ = _priced(pricing, list(prev_usage["rows"]))
    # prev_usage covers both windows; subtract the current one.
    prev_cost = max(0.0, prev_cost - cost)

    accounts = sorted(
        (
            {"account_id": account_id, "requests": s["requests"],
             "tokens": s["prompt_tokens"] + s["completion_tokens"], "error_rate": s["error_rate"]}
            for account_id, s in current["accounts"].items()
        ),
        key=lambda a: a["requests"],
        reverse=True,
    )
    alerts = await evaluate(redis)
    return {
        "days": days,
        "totals": current["totals"],
        "previous": previous,
        "daily": current["daily"],
        "active_users": len(current["accounts"]),
        "top_users": accounts[:5],
        "pool": util["aggregate"],
        "cost": {
            "currency": pricing.currency,
            "total": round(cost, 6),
            "previous": round(prev_cost, 6),
            "unpriced_tokens": unpriced,
        },
        "alerts": {"count": len(alerts), "critical": sum(a["severity"] == "critical" for a in alerts)},
    }


@router.get("/stats")
async def get_stats(
    store: Store,
    operator: Operator,
    days: Annotated[int, Query(ge=1, le=366)] = 7,
) -> dict:
    """Latency percentiles, error classes, per-key history and per-provider/model reliability."""
    return await StatsStore(store.redis).daily(days)


@router.get("/stats/hourly")
async def get_hourly(
    store: Store,
    operator: Operator,
    hours: Annotated[int, Query(ge=1, le=336)] = 48,
) -> dict:
    """Traffic per hour, oldest first, for peak-hour views."""
    return await StatsStore(store.redis).hourly(hours)


@router.get("/accounts/{account_id}/stats")
async def account_stats(
    account_id: Annotated[str, Path(max_length=128)],
    store: Store,
    operator: Operator,
    days: Annotated[int, Query(ge=1, le=90)] = 30,
) -> dict:
    """One user: trend, models, recent requests, sessions, today's cap use, last seen."""
    account = await store.get_account(account_id)
    if account is None:
        raise GatewayError(
            message="Account not found.",
            status_code=404,
            error_type="invalid_request_error",
            code="account_not_found",
        )
    redis = store.redis
    series = await StatsStore(redis).account_days(account_id, days)
    usage = await UsageStore(redis).usage(days)
    models = [r for r in usage["rows"] if r["account_id"] == account_id]
    pricing = await PricingStore(redis).get()
    cost, unpriced = _priced(pricing, models)
    recent = await UsageStore(redis).recent(20, account_id)
    sessions = await SessionStore(redis).list_sessions(account_id)
    keys = await store.list_keys(account_id)
    last_used = max((k.get("last_used_at") or "" for k in keys), default="") or None
    cap = default_user_cap()
    today = series[-1]["requests"] if series else 0
    totals = _sum_days(series)
    merged = [sum(col) for col in zip(*(d["latency_buckets"] for d in series), strict=True)]
    totals["p50_ms"] = percentile(merged, 0.5)
    totals["p95_ms"] = percentile(merged, 0.95)
    return {
        "account_id": account_id,
        "display_name": account.get("display_name") or "",
        "status": account.get("status") or "unknown",
        "created_at": account.get("created_at") or "",
        "last_used_at": last_used,
        "key_count": len(keys),
        "session_count": len(sessions),
        "days": days,
        "daily": series,
        "totals": totals,
        "models": models,
        "recent": recent,
        "cost": {"currency": pricing.currency, "total": round(cost, 6), "unpriced_tokens": unpriced},
        "cap": {"daily_cap": cap, "used_today": today, "pct": round(today / cap * 100, 1) if cap else None},
    }


# -- Costs -------------------------------------------------------------------


@router.get("/pricing")
async def get_pricing(store: Store, operator: Operator) -> dict:
    return (await PricingStore(store.redis).get()).model_dump()


@router.put("/pricing")
async def put_pricing(body: Pricing, store: Store, operator: Operator) -> dict:
    """Replace the price table and monthly budget."""
    saved = await PricingStore(store.redis).set(body)
    await audit(store.redis, operator["id"], "pricing.update", None,
                {"models": len(saved.prices), "monthly_budget": saved.monthly_budget})
    return saved.model_dump()


@router.get("/costs")
async def get_costs(
    store: Store,
    operator: Operator,
    days: Annotated[int, Query(ge=1, le=366)] = 30,
) -> dict:
    """Spend by day, model and user over ``days``, plus month-to-date against the budget."""
    redis = store.redis
    pricing = await PricingStore(redis).get()
    usage = await UsageStore(redis).usage(days, detail=True)

    by_day: dict[str, float] = {}
    by_model: dict[tuple[str, str], dict[str, Any]] = {}
    by_account: dict[str, dict[str, Any]] = {}
    unpriced: dict[str, int] = {}
    total = 0.0
    for row in usage["daily_rows"]:
        cost = cost_of(pricing, row["model"], row["prompt_tokens"], row["completion_tokens"])
        tokens = row["prompt_tokens"] + row["completion_tokens"]
        if cost is None:
            unpriced[row["model"]] = unpriced.get(row["model"], 0) + tokens
            cost = 0.0
        total += cost
        by_day[row["day"]] = by_day.get(row["day"], 0.0) + cost
        m = by_model.setdefault((row["provider"], row["model"]), {"requests": 0, "tokens": 0, "cost": 0.0})
        m["requests"] += row["requests"]
        m["tokens"] += tokens
        m["cost"] += cost
        a = by_account.setdefault(row["account_id"], {"requests": 0, "tokens": 0, "cost": 0.0})
        a["requests"] += row["requests"]
        a["tokens"] += tokens
        a["cost"] += cost

    now = datetime.now(UTC)
    month_prefix = now.strftime("%Y-%m")
    month_cost = sum(c for d, c in by_day.items() if d.startswith(month_prefix))
    if days < now.day:
        # The window is shorter than the month so far: read the full month.
        month_usage = await UsageStore(redis).usage(now.day, detail=True)
        month_cost = 0.0
        for row in month_usage["daily_rows"]:
            if row["day"].startswith(month_prefix):
                month_cost += cost_of(pricing, row["model"], row["prompt_tokens"],
                                      row["completion_tokens"]) or 0.0
    elapsed = now.day + now.hour / 24
    days_in_month = 31 if now.month == 12 else (datetime(now.year, now.month + 1, 1) - datetime(now.year, now.month, 1)).days
    budget = pricing.monthly_budget
    return {
        "currency": pricing.currency,
        "days": days,
        "total": round(total, 6),
        "daily": [{"day": d, "cost": round(c, 6)} for d, c in sorted(by_day.items())],
        "models": sorted(
            ({"provider": p, "model": m, **{k: (round(v, 6) if k == "cost" else v) for k, v in vals.items()}}
             for (p, m), vals in by_model.items()),
            key=lambda r: r["cost"], reverse=True),
        "accounts": sorted(
            ({"account_id": a, **{k: (round(v, 6) if k == "cost" else v) for k, v in vals.items()}}
             for a, vals in by_account.items()),
            key=lambda r: r["cost"], reverse=True),
        "unpriced": [{"model": m, "tokens": t} for m, t in sorted(unpriced.items(), key=lambda i: -i[1])],
        "month": {
            "month": month_prefix,
            "cost": round(month_cost, 6),
            "projected": round(month_cost / elapsed * days_in_month, 6) if elapsed else 0.0,
            "budget": budget,
            "budget_used_pct": round(month_cost / budget * 100, 1) if budget else None,
        },
    }


# -- Alerts ------------------------------------------------------------------


@router.get("/alerts")
async def active_alerts(store: Store, operator: Operator) -> dict:
    """Alerts firing right now."""
    alerts = await evaluate(store.redis)
    return {"object": "list", "data": alerts}


@router.get("/alerts/rules")
async def get_alert_rules(store: Store, operator: Operator) -> dict:
    return (await AlertStore(store.redis).get()).model_dump()


@router.put("/alerts/rules")
async def put_alert_rules(body: AlertRules, store: Store, operator: Operator) -> dict:
    saved = await AlertStore(store.redis).set(body)
    await audit(store.redis, operator["id"], "alerts.update", None,
                {"enabled": saved.enabled, "webhook": bool(saved.webhook_url)})
    return saved.model_dump()


@router.post("/alerts/test")
async def test_alert_webhook(store: Store, operator: Operator) -> dict:
    """Send one test message to the configured webhook."""
    rules = await AlertStore(store.redis).get()
    if not rules.webhook_url:
        raise GatewayError(message="No webhook URL is set.", status_code=409,
                           error_type="invalid_request_error", code="no_webhook")
    probe = [{"id": f"test:{time.time_ns()}", "severity": "warning", "title": "Test alert",
              "detail": "This is a test from Palimpsest Gateway."}]
    try:
        await deliver(store.redis, probe, rules.model_copy(update={"enabled": True}))
    except Exception as exc:
        raise GatewayError(message=f"Webhook delivery failed: {type(exc).__name__}.", status_code=502,
                           error_type="api_error", code="webhook_failed") from exc
    return {"sent": True}


# -- Audit -------------------------------------------------------------------


@router.get("/audit")
async def get_audit(
    store: Store,
    operator: Operator,
    limit: Annotated[int, Query(ge=1, le=1000)] = 200,
    action: Annotated[str | None, Query(max_length=64)] = None,
) -> dict:
    """Operator actions, newest first."""
    return {"object": "list", "data": await recent_audit(store.redis, limit, action)}


# -- Export ------------------------------------------------------------------


def _csv(header: list[str], rows: list[list[Any]], name: str) -> Response:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(header)
    for row in rows:
        # Spreadsheet formula injection: neutralise cells that start like a formula.
        writer.writerow(
            ["'" + c if isinstance(c, str) and c[:1] in ("=", "+", "-", "@") else c for c in row]
        )
    return Response(
        buffer.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


@router.get("/export/usage.csv")
async def export_usage(
    store: Store,
    operator: Operator,
    days: Annotated[int, Query(ge=1, le=366)] = 30,
) -> Response:
    pricing = await PricingStore(store.redis).get()
    data = await UsageStore(store.redis).usage(days, detail=True)
    rows = []
    for r in data["daily_rows"]:
        cost = cost_of(pricing, r["model"], r["prompt_tokens"], r["completion_tokens"])
        rows.append([r["day"], r["account_id"], r["provider"], r["model"], r["requests"],
                     r["prompt_tokens"], r["completion_tokens"],
                     "" if cost is None else round(cost, 6)])
    return _csv(["day", "account_id", "provider", "model", "requests", "prompt_tokens",
                 "completion_tokens", f"cost_{pricing.currency}"], rows, f"usage-{days}d.csv")


@router.get("/export/requests.csv")
async def export_requests(
    store: Store,
    operator: Operator,
    limit: Annotated[int, Query(ge=1, le=10000)] = 1000,
    account_id: Annotated[str | None, Query(max_length=128)] = None,
) -> Response:
    data = await UsageStore(store.redis).recent(limit, account_id)
    header = ["timestamp", "request_id", "account_id", "session_id", "provider", "model", "status_code",
              "outcome", "latency_ms", "stream", "attempts", "prompt_tokens", "completion_tokens"]
    return _csv(header, [[r.get(h) for h in header] for r in data], "requests.csv")


@router.get("/export/audit.csv")
async def export_audit(store: Store, operator: Operator) -> Response:
    data = await recent_audit(store.redis, 5000)
    return _csv(["ts", "operator_id", "action", "target", "detail"],
                [[r["ts"], r["operator_id"], r["action"], r.get("target"), str(r.get("detail"))] for r in data],
                "audit.csv")


# -- System ------------------------------------------------------------------


@router.get("/system")
async def system(request: Request, store: Store, operator: Operator) -> dict:
    """Process and Redis health, retention and storage footprint."""
    settings = get_settings()
    redis = store.redis
    info: dict[str, Any] = {}
    try:
        info = await redis.info()
    except Exception:
        info = {}
    ping_ms = None
    try:
        start = time.perf_counter()
        await redis.ping()
        ping_ms = round((time.perf_counter() - start) * 1000, 2)
    except Exception:
        pass
    try:
        keys = await redis.dbsize()
    except Exception:
        keys = None

    def pick(name: str) -> Any:
        return info.get(name)

    return {
        "version": settings.project_version,
        "uptime_seconds": max(0, int(datetime.now(UTC).timestamp()) - request.app.state.created_at),
        "redis": {
            "reachable": ping_ms is not None,
            "ping_ms": ping_ms,
            "version": pick("redis_version"),
            "used_memory_bytes": pick("used_memory"),
            "peak_memory_bytes": pick("used_memory_peak"),
            "max_memory_bytes": pick("maxmemory") or None,
            "connected_clients": pick("connected_clients"),
            "uptime_seconds": pick("uptime_in_seconds"),
            "total_keys": keys,
            "evicted_keys": pick("evicted_keys"),
        },
        "retention": {
            "usage_retention_days": settings.usage_retention_days,
            "usage_recent_max": settings.usage_recent_max,
            "hourly_retention_hours": 24 * 14,
            "failover_event_log_size": settings.failover_event_log_size,
        },
    }


@router.get("/metrics", response_class=PlainTextResponse)
async def metrics(request: Request, store: Store, operator: Operator) -> PlainTextResponse:
    """Prometheus text format; authenticate the scraper with an operator Bearer token."""
    redis = store.redis
    stats = await StatsStore(redis).daily(1)
    hour = (await StatsStore(redis).hourly(1))["hourly"][-1]
    util = await utilization_view(redis=redis)
    agg = util["aggregate"]
    lines: list[str] = []

    def metric(name: str, kind: str, help_: str, samples: list[tuple[dict[str, str], float | int]]) -> None:
        lines.append(f"# HELP {name} {help_}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            tag = ",".join(f'{k}="{str(v).replace(chr(92), "").replace(chr(34), "")}"' for k, v in labels.items())
            lines.append(f"{name}{{{tag}}} {value}" if tag else f"{name} {value}")

    t = stats["totals"]
    metric("palimpsest_requests_today", "gauge", "Requests since UTC midnight.", [({}, t["requests"])])
    metric("palimpsest_errors_today", "gauge", "4xx and 5xx responses since UTC midnight.",
           [({"class": "4xx"}, t["errors_4xx"]), ({"class": "5xx"}, t["errors_5xx"])])
    metric("palimpsest_tokens_today", "gauge", "Tokens since UTC midnight.",
           [({"kind": "prompt"}, t["prompt_tokens"]), ({"kind": "completion"}, t["completion_tokens"])])
    metric("palimpsest_requests_last_hour", "gauge", "Requests in the current UTC hour.", [({}, hour["requests"])])
    for q, field in (("0.5", "p50_ms"), ("0.95", "p95_ms"), ("0.99", "p99_ms")):
        if t[field] is not None:
            metric(f"palimpsest_latency_ms_{field[:-3]}_today", "gauge",
                   f"Latency quantile {q} in ms since UTC midnight.", [({}, t[field])])
    metric("palimpsest_keys", "gauge", "Pool keys by state.",
           [({"state": s}, agg[f"{s}_keys"]) for s in ("active", "exhausted", "cooldown", "suspended")])
    if agg.get("utilization_pct") is not None:
        metric("palimpsest_pool_utilization_percent", "gauge", "Capped pool utilization.",
               [({}, agg["utilization_pct"])])
    metric("palimpsest_uptime_seconds", "gauge", "Process uptime.",
           [({}, max(0, int(datetime.now(UTC).timestamp()) - request.app.state.created_at))])
    return PlainTextResponse("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")
