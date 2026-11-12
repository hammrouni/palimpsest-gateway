"""Operator view of supermode: how it is set, what it did, and how it ranks models.

Metadata only, like the rest of the operator realm: model, provider and account
ids, counts and scores. Never message content or key material.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_store
from app.api.operator_deps import get_current_operator
from app.config import get_settings
from app.pool.registry import PoolRegistry
from app.router import supermode_stats
from app.router.learning import Learned, Needs, learned_key_limits
from app.router.supermode import SUPERMODE_MODEL, RankEntry, explain_ranking
from app.store.redis_store import RedisStore

router = APIRouter(prefix="/operator/v1", tags=["operator"])

Store = Annotated[RedisStore, Depends(get_store)]
Operator = Annotated[dict, Depends(get_current_operator)]

# What a typical request looks like, for a ranking nobody asked a question of.
_DEFAULT_TOKENS = 2000


def _learned(facts: Learned) -> dict[str, Any]:
    return {
        "context_proven": facts.ctx_ok,
        "context_refused_at": facts.ctx_fail,
        "vision": facts.vision,
        "tools": facts.tools,
        "ok": facts.ok,
        "failed": facts.fail,
        "throttled_at": facts.throttled_at,
        "declined_at": facts.declined_at,
    }


def _entry(entry: RankEntry, position: int | None) -> dict[str, Any]:
    return {
        "position": position,
        "model": entry.model,
        "providers": list(entry.providers),
        "base_quality": entry.base_quality,
        "override": entry.override,
        "penalty": entry.penalty,
        "score": entry.score,
        "context": entry.context,
        "vision": entry.vision,
        "tools": entry.tools,
        "keys_ready": entry.keys_ready,
        "capacity": entry.capacity,
        "dropped_because": entry.reason,
        "learned": _learned(entry.learned),
    }


@router.get("/supermode/ranking")
async def supermode_ranking(
    store: Store,
    operator: Operator,
    tokens: Annotated[int, Query(ge=1, le=10_000_000)] = _DEFAULT_TOKENS,
    vision: bool = False,
    tools: bool = False,
) -> dict:
    """The ranking supermode would use right now for a request of this size and needs."""
    entries = await explain_ranking(
        PoolRegistry(store.redis), Needs(tokens=tokens, vision=vision, tools=tools)
    )
    position = 0
    rows = []
    for entry in entries:
        if entry.ranked:
            position += 1
        rows.append(_entry(entry, position if entry.ranked else None))
    return {
        "needs": {"tokens": tokens, "vision": vision, "tools": tools},
        "ranked": position,
        "dropped": len(rows) - position,
        "models": rows,
    }


@router.get("/supermode")
async def supermode_overview(
    store: Store,
    operator: Operator,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> dict:
    """Settings, statistics and recent requests for supermode."""
    settings = get_settings()
    redis = store.redis
    registry = PoolRegistry(redis)
    records = await registry.list()
    limits = await _learned_limits(redis, records)
    return {
        "model": SUPERMODE_MODEL,
        "config": {
            "fallback_models": settings.supermode_fallback_models,
            "quality_overrides": dict(settings.supermode_quality),
            "master_model": settings.master_model,
            "bypassed": bool(settings.master_model),
        },
        "stats": await supermode_stats.summary(redis, days),
        "recent": await supermode_stats.recent(redis, 50),
        "learned_limits": limits,
    }


async def _learned_limits(redis: Any, records: list) -> list[dict[str, Any]]:
    """Requests-per-minute limits the gateway worked out for keys with no rate-limit headers."""
    limits = await learned_key_limits(redis, [record.id for record in records])
    rows = [
        {
            "key_id": record.id,
            "label": record.label,
            "provider": record.provider.value,
            "requests_per_minute": limits[record.id],
        }
        for record in records
        if record.id in limits
    ]
    return sorted(rows, key=lambda row: (row["provider"], row["key_id"]))
