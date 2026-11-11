"""Aggregate counters behind the operator's analytics: latency, errors, per-key
history and hourly traffic.

Metadata only. Unlike the token counters in ``UsageStore``, these take every
outcome, so every dimension is bounded: providers (a handful), pool key ids,
account ids, failure classes, and models only for served requests.

Layout in Redis, one hash per UTC bucket:

* ``stats:day:<YYYYMMDD>`` expires after ``usage_retention_days``.
* ``stats:hour:<YYYYMMDDHH>`` expires after ``HOURLY_RETENTION_HOURS``.

Fields are ``|``-separated: ``<scope>|<name>|<metric>``. Metrics are ``req``,
``ok``, ``e4`` / ``e5`` (4xx / 5xx responses), ``ex`` (pool exhausted), ``fo``
(served after a failover), ``fail`` (failed provider tries), ``pt`` / ``ct``
(tokens), ``lsum`` (latency sum, ms) and ``lb<i>`` (latency histogram bucket).

``th`` counts throttled requests: refused because no key had capacity or the
user reached a cap, before any provider answered. They are not errors (``e4`` /
``e5``) and carry no latency, so they never drag the error rate, success rate
or percentiles. ``rep`` counts repeats of a refusal already recorded for the
same client and model within its Retry-After window; a repeat adds nothing else.
"""
from __future__ import annotations

import bisect
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_settings

HOURLY_RETENTION_HOURS = 24 * 14

# Upper edges in ms; the last bucket is open-ended.
LATENCY_EDGES = (50, 100, 200, 400, 800, 1600, 3200, 6400, 12800, 25600, 51200)

_DAY = "stats:day:{day}"
_HOUR = "stats:hour:{hour}"
_UNKNOWN = "unknown"
_MODEL_MAX = 64
_MAX_DAYS = 366
_MAX_HOURS = 24 * 14

# Refusals for lack of capacity: the client is told to wait, nothing failed.
THROTTLED_OUTCOMES = frozenset({"exhausted_429", "rejected_user_cap", "rejected_pool_low_water"})
# The pool had no key left for the request, whatever status it was given.
_POOL_EXHAUSTED_OUTCOMES = frozenset({"exhausted_429", "exhausted_503"})


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


def _bucket(latency_ms: float) -> int:
    return bisect.bisect_left(LATENCY_EDGES, latency_ms)


def percentile(buckets: list[int], q: float) -> float | None:
    """Estimate the ``q`` quantile (0-1) from histogram counts, interpolating in-bucket."""
    total = sum(buckets)
    if total <= 0:
        return None
    target = q * total
    running = 0
    for index, count in enumerate(buckets):
        if count and running + count >= target:
            low = LATENCY_EDGES[index - 1] if index > 0 else 0
            high = LATENCY_EDGES[index] if index < len(LATENCY_EDGES) else low * 2
            fraction = (target - running) / count
            return round(low + (high - low) * fraction, 1)
        running += count
    return float(LATENCY_EDGES[-1])


def _empty_buckets() -> list[int]:
    return [0] * (len(LATENCY_EDGES) + 1)


def _fields(
    *,
    account_id: str,
    provider: str,
    model: str,
    key_id: str | None,
    status_code: int,
    outcome: str,
    latency_ms: int,
    attempts: int,
    prompt_tokens: int | None,
    completion_tokens: int | None,
    failures: list[dict[str, Any]],
    served: bool,
    clean: bool,
    ttft_ms: int | None = None,
) -> dict[str, int]:
    """Counter increments for one request."""
    out: dict[str, int] = {}

    def bump(name: str, amount: int = 1) -> None:
        out[name] = out.get(name, 0) + amount

    bucket = _bucket(latency_ms)
    scopes = ["all|-", f"acct|{account_id}", f"prov|{provider}"]
    if served:
        scopes.append(f"model|{provider}|{model}")
    if key_id:
        scopes.append(f"key|{key_id}")

    throttled = outcome in THROTTLED_OUTCOMES
    for scope in scopes:
        bump(f"{scope}|req")
        if outcome in _POOL_EXHAUSTED_OUTCOMES:
            bump(f"{scope}|ex")
        if throttled:
            bump(f"{scope}|th")
            continue
        bump(f"{scope}|lsum", int(latency_ms))
        bump(f"{scope}|lb{bucket}")
        if ttft_ms is not None:
            bump(f"{scope}|tsum", int(ttft_ms))
            bump(f"{scope}|tn")
        if clean:
            bump(f"{scope}|ok")
        if status_code >= 500:
            bump(f"{scope}|e5")
        elif status_code >= 400:
            bump(f"{scope}|e4")
        if served and attempts > 1:
            bump(f"{scope}|fo")
        if prompt_tokens is not None and completion_tokens is not None and served:
            bump(f"{scope}|pt", int(prompt_tokens))
            bump(f"{scope}|ct", int(completion_tokens))

    for failure in failures:
        cls = str(failure.get("classification") or _UNKNOWN)[:40]
        bump(f"err|{cls}|n")
        failed_key = failure.get("key_id")
        if failed_key:
            bump(f"key|{failed_key}|fail")
    return out


class StatsStore:
    """Writes and reads the aggregate counters."""

    def __init__(self, redis: Any) -> None:
        self.redis = redis

    async def record(
        self,
        *,
        account_id: str,
        provider: str | None,
        model: str,
        key_id: str | None,
        status_code: int,
        outcome: str,
        latency_ms: int,
        attempts: int,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        failures: list[dict[str, Any]] | None,
        served: bool,
        clean: bool,
        ttft_ms: int | None = None,
        now: datetime | None = None,
    ) -> None:
        """Add one request to the day and hour hashes."""
        settings = get_settings()
        now = now or datetime.now(UTC)
        fields = _fields(
            account_id=account_id,
            provider=provider or _UNKNOWN,
            model=model[:_MODEL_MAX],
            key_id=key_id,
            status_code=status_code,
            outcome=outcome,
            latency_ms=latency_ms,
            attempts=attempts,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            failures=failures or [],
            served=served,
            clean=clean,
            ttft_ms=ttft_ms,
        )
        day_key = _DAY.format(day=now.strftime("%Y%m%d"))
        hour_key = _HOUR.format(hour=now.strftime("%Y%m%d%H"))
        async with self.redis.pipeline(transaction=False) as pipe:
            for name, amount in fields.items():
                pipe.hincrby(day_key, name, amount)
                # The hourly view needs only the totals and the global histogram.
                if name.startswith("all|-|") or name.startswith("err|"):
                    pipe.hincrby(hour_key, name, amount)
            pipe.expire(day_key, settings.usage_retention_days * 86400)
            pipe.expire(hour_key, HOURLY_RETENTION_HOURS * 3600)
            await pipe.execute()

    async def record_repeat(self, *, account_id: str, now: datetime | None = None) -> None:
        """Count a throttled refusal that repeats one already recorded."""
        settings = get_settings()
        now = now or datetime.now(UTC)
        day_key = _DAY.format(day=now.strftime("%Y%m%d"))
        hour_key = _HOUR.format(hour=now.strftime("%Y%m%d%H"))
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.hincrby(day_key, "all|-|rep", 1)
            pipe.hincrby(day_key, f"acct|{account_id}|rep", 1)
            pipe.hincrby(hour_key, "all|-|rep", 1)
            pipe.expire(day_key, settings.usage_retention_days * 86400)
            pipe.expire(hour_key, HOURLY_RETENTION_HOURS * 3600)
            await pipe.execute()

    # -- reads -------------------------------------------------------------

    async def _hashes(self, keys: list[str]) -> list[dict[str, int]]:
        async with self.redis.pipeline(transaction=False) as pipe:
            for key in keys:
                pipe.hgetall(key)
            raw = await pipe.execute()
        out: list[dict[str, int]] = []
        for item in raw:
            parsed: dict[str, int] = {}
            for field, value in (item or {}).items():
                try:
                    parsed[_text(field)] = int(value)
                except (TypeError, ValueError):
                    continue
            out.append(parsed)
        return out

    @staticmethod
    def _summarize(fields: dict[str, int], scope: str) -> dict[str, Any]:
        """Totals for one scope (``all|-``, ``prov|openai``, ...) out of a flat hash."""
        prefix = scope + "|"
        return StatsStore._summary(
            {name[len(prefix):]: value for name, value in fields.items() if name.startswith(prefix)}
        )

    @staticmethod
    def _group(fields: dict[str, int]) -> dict[str, dict[str, int]]:
        """``{scope: {metric: value}}`` in one pass: the metric is after the last ``|``."""
        out: dict[str, dict[str, int]] = {}
        for name, value in fields.items():
            scope, _, metric = name.rpartition("|")
            out.setdefault(scope, {})[metric] = value
        return out

    @staticmethod
    def _kind(groups: dict[str, dict[str, int]], kind: str) -> dict[str, dict[str, int]]:
        """The scopes of one kind (``prov``, ``key``, ...) by name, from ``_group``."""
        lead = kind + "|"
        return {scope[len(lead):]: m for scope, m in groups.items() if scope.startswith(lead)}

    @staticmethod
    def _summary(metrics: dict[str, int]) -> dict[str, Any]:
        """Totals for one scope's ``{metric: value}``."""
        buckets = _empty_buckets()
        total = {
            "requests": 0, "ok": 0, "errors_4xx": 0, "errors_5xx": 0, "exhausted": 0,
            "throttled": 0, "repeats": 0,
            "failovers": 0, "prompt_tokens": 0, "completion_tokens": 0, "latency_sum_ms": 0,
            "ttft_sum_ms": 0, "ttft_n": 0,
        }
        names = {
            "req": "requests", "ok": "ok", "e4": "errors_4xx", "e5": "errors_5xx",
            "ex": "exhausted", "th": "throttled", "rep": "repeats",
            "fo": "failovers", "pt": "prompt_tokens",
            "ct": "completion_tokens", "lsum": "latency_sum_ms",
            "tsum": "ttft_sum_ms", "tn": "ttft_n",
        }
        for metric, value in metrics.items():
            if metric in names:
                total[names[metric]] += value
            elif metric.startswith("lb") and metric[2:].isdigit():
                index = int(metric[2:])
                if index < len(buckets):
                    buckets[index] += value
        return _finish(total, buckets)

    async def daily(self, days: int, now: datetime | None = None) -> dict[str, Any]:
        """Per-day totals plus ranges merged over the window, by every dimension."""
        days = max(1, min(days, _MAX_DAYS))
        today = (now or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_list = [today - timedelta(days=offset) for offset in range(days)]
        hashes = await self._hashes([_DAY.format(day=d.strftime("%Y%m%d")) for d in day_list])

        # Each hash is grouped by scope once; summarizing a scope then touches
        # only its own fields instead of rescanning every field in the window.
        merged: dict[str, int] = {}
        per_day: list[dict[str, Any]] = []
        key_days: dict[str, list[dict[str, int]]] = {}
        for moment, fields in zip(day_list, hashes, strict=True):
            for name, value in fields.items():
                merged[name] = merged.get(name, 0) + value
            groups = self._group(fields)
            day = moment.date().isoformat()
            per_day.append({"day": day, **self._summary(groups.get("all|-", {}))})
            per_day[-1]["error_classes"] = {
                name: metrics.get("n", 0) for name, metrics in self._kind(groups, "err").items()
            }
            for key_id, metrics in self._kind(groups, "key").items():
                key_days.setdefault(key_id, []).append(
                    {
                        "day": day,
                        "requests": metrics.get("req", 0),
                        "tokens": metrics.get("pt", 0) + metrics.get("ct", 0),
                        "failed_tries": metrics.get("fail", 0),
                    }
                )
        per_day.reverse()

        groups = self._group(merged)
        providers = {name: self._summary(m) for name, m in self._kind(groups, "prov").items()}
        models = []
        for name, metrics in self._kind(groups, "model").items():
            provider, _, model = name.partition("|")
            models.append({"provider": provider, "model": model, **self._summary(metrics)})
        models.sort(key=lambda item: item["requests"], reverse=True)

        keys = []
        for key_id, metrics in self._kind(groups, "key").items():
            summary = self._summary(metrics)
            failed = metrics.get("fail", 0)
            keys.append(
                {
                    "key_id": key_id,
                    **summary,
                    "failed_tries": failed,
                    "failure_rate": round(failed / (summary["requests"] + failed), 4)
                    if summary["requests"] + failed else 0.0,
                    "days": sorted(key_days.get(key_id, []), key=lambda d: d["day"]),
                }
            )
        keys.sort(key=lambda item: item["failed_tries"], reverse=True)

        accounts = {name: self._summary(m) for name, m in self._kind(groups, "acct").items()}
        error_classes = {name: m.get("n", 0) for name, m in self._kind(groups, "err").items()}
        return {
            "days": days,
            "since": day_list[-1].date().isoformat(),
            "totals": self._summary(groups.get("all|-", {})),
            "daily": per_day,
            "providers": providers,
            "models": models,
            "keys": keys,
            "accounts": accounts,
            "error_classes": error_classes,
            "latency_edges_ms": list(LATENCY_EDGES),
        }

    async def totals_between(
        self, days: int, skip: int = 0, now: datetime | None = None
    ) -> dict[str, Any]:
        """Pool-wide totals over ``days`` UTC days, ending ``skip`` days before today.

        Reads only the ``all|-`` fields (HMGET), not whole day hashes, which hold
        every account, key and model: comparing with a previous window is cheap.
        """
        days = max(1, min(days, _MAX_DAYS))
        today = (now or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_list = [today - timedelta(days=skip + offset) for offset in range(days)]
        metrics = ["req", "ok", "e4", "e5", "ex", "th", "rep", "fo", "pt", "ct", "lsum", "tsum", "tn"]
        metrics += [f"lb{i}" for i in range(len(LATENCY_EDGES) + 1)]
        names = [f"all|-|{metric}" for metric in metrics]
        async with self.redis.pipeline(transaction=False) as pipe:
            for moment in day_list:
                pipe.hmget(_DAY.format(day=moment.strftime("%Y%m%d")), names)
            replies = await pipe.execute()
        merged: dict[str, int] = {}
        for values in replies:
            for name, value in zip(names, values or [], strict=False):
                if value is None:
                    continue
                try:
                    merged[name] = merged.get(name, 0) + int(value)
                except (TypeError, ValueError):
                    continue
        return self._summarize(merged, "all|-")

    async def hourly(self, hours: int, now: datetime | None = None) -> dict[str, Any]:
        """Per-hour totals, oldest first, for the last ``hours`` hours (current included)."""
        hours = max(1, min(hours, _MAX_HOURS))
        current = (now or datetime.now(UTC)).replace(minute=0, second=0, microsecond=0)
        hour_list = [current - timedelta(hours=offset) for offset in range(hours)]
        hashes = await self._hashes([_HOUR.format(hour=h.strftime("%Y%m%d%H")) for h in hour_list])
        rows = [
            {"hour": h.isoformat(), **self._summarize(fields, "all|-")}
            for h, fields in zip(hour_list, hashes, strict=True)
        ]
        rows.reverse()
        return {"hours": hours, "hourly": rows}

    async def account_days(
        self, account_id: str, days: int, now: datetime | None = None
    ) -> list[dict[str, Any]]:
        """One account's per-day totals, oldest first."""
        days = max(1, min(days, _MAX_DAYS))
        today = (now or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_list = [today - timedelta(days=offset) for offset in range(days)]
        hashes = await self._hashes([_DAY.format(day=d.strftime("%Y%m%d")) for d in day_list])
        rows = [
            {"day": d.date().isoformat(), **self._summarize(fields, f"acct|{account_id}")}
            for d, fields in zip(day_list, hashes, strict=True)
        ]
        rows.reverse()
        return rows


def _finish(total: dict[str, int], buckets: list[int]) -> dict[str, Any]:
    # Rates and latency are over the requests the pool took on: a throttled
    # refusal is neither a success nor a failure, and took no provider time.
    handled = total["requests"] - total["throttled"]
    return {
        **total,
        "handled": handled,
        "success_rate": round(total["ok"] / handled, 4) if handled > 0 else None,
        "error_rate": round((total["errors_4xx"] + total["errors_5xx"]) / handled, 4)
        if handled > 0 else None,
        "avg_latency_ms": round(total["latency_sum_ms"] / handled, 1) if handled > 0 else None,
        "avg_ttft_ms": round(total["ttft_sum_ms"] / total["ttft_n"], 1) if total["ttft_n"] else None,
        "p50_ms": percentile(buckets, 0.50),
        "p95_ms": percentile(buckets, 0.95),
        "p99_ms": percentile(buckets, 0.99),
        "latency_buckets": buckets,
    }
