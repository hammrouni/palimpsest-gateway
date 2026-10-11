"""Per-account, per-model token usage and a capped list of recent requests.

Operator-only metadata: ids, provider and model names, counts and timings.
Never message content or key material.

Layout in Redis:

* ``usage:day:<YYYYMMDD>`` is one hash per UTC day. Each field is
  ``<account_id>|<provider>|<model>|<metric>`` with metric one of ``req`` (served
  requests), ``ok`` (completed cleanly), ``pt`` / ``ct`` (prompt / completion
  tokens) and ``nu`` (requests whose provider reported no usage). The hash
  expires after ``usage_retention_days``.
* ``usage:recent`` is a list of JSON records, newest first, trimmed to
  ``usage_recent_max``.

Only requests a provider actually served feed the day hashes, so the model
string is one the pool vets and a client cannot grow the hash with arbitrary
names. The recent list takes every outcome but is length-capped and truncates
the model string.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Coroutine
from datetime import UTC, datetime, timedelta
from typing import Any

from app.config import get_settings
from app.usage.stats import StatsStore

logger = logging.getLogger("palimpsest.usage")

# One hash per UTC day; fields are ``account|provider|model|metric``.
_DAY_KEY = "usage:day:{day}"
_UNKNOWN = "unknown"
_RECENT_KEY = "usage:recent"
_MODEL_MAX = 64
_MAX_RANGE_DAYS = 366

# Outcomes where a provider served the request, so the model string is trusted.
SERVED_OUTCOMES = frozenset({"success", "success_persist_failed", "stream_interrupted"})
_CLEAN_OUTCOMES = frozenset({"success", "success_persist_failed"})

_pending: set[asyncio.Task] = set()


def track(coro: Coroutine[Any, Any, Any]) -> None:
    """Run a bookkeeping coroutine without delaying or failing the request.

    The task is held in a set so it is not garbage-collected mid-flight, and any
    error is logged and dropped: statistics must never change a client outcome.
    """

    async def guarded() -> None:
        try:
            await coro
        except Exception:
            logger.error("usage recording failed", exc_info=True)

    try:
        task = asyncio.get_running_loop().create_task(guarded())
    except RuntimeError:
        coro.close()
        return
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def drain() -> None:
    """Wait for this event loop's in-flight recording tasks.

    Tasks left behind by a closed loop can never finish, so they are dropped
    rather than awaited.
    """
    loop = asyncio.get_running_loop()
    _pending.difference_update({t for t in _pending if t.get_loop().is_closed()})
    while True:
        tasks = [t for t in _pending if t.get_loop() is loop and not t.done()]
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)


def _day(moment: datetime) -> str:
    return moment.strftime("%Y%m%d")


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


class UsageStore:
    def __init__(self, redis: Any) -> None:
        self.redis = redis

    async def record_request(
        self,
        *,
        request_id: str,
        account_id: str,
        session_id: str | None,
        model: str,
        status_code: int,
        outcome: str,
        latency_ms: int,
        stream: bool,
        attempts: int,
        prompt_tokens: int | None,
        completion_tokens: int | None,
        failures: list[dict[str, Any]] | None = None,
        provider: str | None = None,
        key_id: str | None = None,
        ttft_ms: int | None = None,
        now: datetime | None = None,
    ) -> None:
        """Append one recent-request record and, if served, bump the day counters."""
        settings = get_settings()
        now = now or datetime.now(UTC)
        model = model[:_MODEL_MAX]
        known = prompt_tokens is not None and completion_tokens is not None
        record = {
            "timestamp": now.isoformat(),
            "request_id": request_id,
            "account_id": account_id,
            "session_id": session_id,
            "model": model,
            "provider": provider or None,
            "status_code": status_code,
            "outcome": outcome,
            "latency_ms": latency_ms,
            "stream": stream,
            "attempts": attempts,
            # One entry per failed provider try: HTTP status and how the gateway
            # classified it. Never the provider's message or any key.
            "failures": [{k: v for k, v in f.items() if k != "key_id"} for f in failures or []],
            "ttft_ms": ttft_ms,
            "prompt_tokens": prompt_tokens if known else None,
            "completion_tokens": completion_tokens if known else None,
        }
        async with self.redis.pipeline(transaction=False) as pipe:
            pipe.lpush(_RECENT_KEY, json.dumps(record))
            pipe.ltrim(_RECENT_KEY, 0, settings.usage_recent_max - 1)
            if outcome in SERVED_OUTCOMES:
                key = _DAY_KEY.format(day=_day(now))
                prefix = f"{account_id}|{provider or _UNKNOWN}|{model}|"
                pipe.hincrby(key, prefix + "req", 1)
                if outcome in _CLEAN_OUTCOMES:
                    pipe.hincrby(key, prefix + "ok", 1)
                if known:
                    pipe.hincrby(key, prefix + "pt", int(prompt_tokens))
                    pipe.hincrby(key, prefix + "ct", int(completion_tokens))
                else:
                    pipe.hincrby(key, prefix + "nu", 1)
                pipe.expire(key, settings.usage_retention_days * 86400)
            await pipe.execute()
        # Aggregate analytics (latency, errors, per-key, hourly) take every outcome.
        await StatsStore(self.redis).record(
            account_id=account_id,
            provider=provider,
            model=model,
            key_id=key_id,
            status_code=status_code,
            outcome=outcome,
            latency_ms=latency_ms,
            attempts=attempts,
            prompt_tokens=prompt_tokens if known else None,
            completion_tokens=completion_tokens if known else None,
            failures=failures,
            served=outcome in SERVED_OUTCOMES,
            clean=outcome in _CLEAN_OUTCOMES,
            ttft_ms=ttft_ms,
            now=now,
        )

    async def usage(
        self, days: int, now: datetime | None = None, *, detail: bool = False
    ) -> dict[str, Any]:
        """Token usage over the last ``days`` UTC days (today included).

        Returns per-account-per-model ``rows`` plus per-day totals in ``daily``.
        """
        days = max(1, min(days, _MAX_RANGE_DAYS))
        today = (now or datetime.now(UTC)).replace(hour=0, minute=0, second=0, microsecond=0)
        day_list = [today - timedelta(days=offset) for offset in range(days)]

        async with self.redis.pipeline(transaction=False) as pipe:
            for moment in day_list:
                pipe.hgetall(_DAY_KEY.format(day=_day(moment)))
            hashes = await pipe.execute()

        rows: dict[tuple[str, str, str], dict[str, int]] = {}
        daily: list[dict[str, Any]] = []
        # Per-day rows by account, provider and model; only when asked (cost views).
        daily_rows: dict[tuple[str, str, str, str], dict[str, int]] = {}
        for index, moment in enumerate(day_list):
            totals = {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0}
            for field, value in (hashes[index] or {}).items():
                # Model strings may contain '|': take the account and provider
                # from the left and the metric from the right.
                account_id, _, rest = _text(field).partition("|")
                middle, _, metric = rest.rpartition("|")
                provider, _, model = middle.partition("|")
                row = rows.setdefault(
                    (account_id, provider, model),
                    {
                        "requests": 0,
                        "ok": 0,
                        "prompt_tokens": 0,
                        "completion_tokens": 0,
                        "requests_without_usage": 0,
                    },
                )
                try:
                    count = int(value)
                except (TypeError, ValueError):
                    continue
                if detail and metric in ("req", "pt", "ct"):
                    cell = daily_rows.setdefault(
                        (moment.date().isoformat(), account_id, provider, model),
                        {"requests": 0, "prompt_tokens": 0, "completion_tokens": 0},
                    )
                    cell[{"req": "requests", "pt": "prompt_tokens", "ct": "completion_tokens"}[metric]] += count
                if metric == "req":
                    row["requests"] += count
                    totals["requests"] += count
                elif metric == "ok":
                    row["ok"] += count
                elif metric == "pt":
                    row["prompt_tokens"] += count
                    totals["prompt_tokens"] += count
                elif metric == "ct":
                    row["completion_tokens"] += count
                    totals["completion_tokens"] += count
                elif metric == "nu":
                    row["requests_without_usage"] += count
            daily.append({"day": moment.date().isoformat(), **totals})

        out_rows = [
            {
                "account_id": account_id,
                "provider": provider,
                "model": model,
                **row,
                "total_tokens": row["prompt_tokens"] + row["completion_tokens"],
            }
            for (account_id, provider, model), row in rows.items()
        ]
        out_rows.sort(key=lambda r: r["total_tokens"], reverse=True)
        daily.reverse()  # oldest first, for charting
        result = {
            "days": days,
            "since": day_list[-1].date().isoformat(),
            "rows": out_rows,
            "daily": daily,
        }
        if detail:
            result["daily_rows"] = [
                {"day": d, "account_id": a, "provider": p, "model": m, **v}
                for (d, a, p, m), v in sorted(daily_rows.items())
            ]
        return result

    async def recent(self, limit: int, account_id: str | None = None) -> list[dict[str, Any]]:
        """Most recent requests first; optionally only one account's."""
        limit = max(1, limit)
        settings = get_settings()
        raw = await self.redis.lrange(_RECENT_KEY, 0, settings.usage_recent_max - 1)
        out: list[dict[str, Any]] = []
        for item in raw:
            try:
                record = json.loads(_text(item))
            except ValueError:
                continue
            if account_id and record.get("account_id") != account_id:
                continue
            out.append(record)
            if len(out) >= limit:
                break
        return out
