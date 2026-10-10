"""Operator-realm statistics: token usage by account and model, recent requests.

Metadata only: ids, model strings, counts and timings, never message content.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_store
from app.api.operator_deps import get_current_operator
from app.store.redis_store import RedisStore
from app.usage.store import UsageStore

router = APIRouter(prefix="/operator/v1", tags=["operator"])


@router.get("/usage")
async def get_usage(
    store: Annotated[RedisStore, Depends(get_store)],
    operator: Annotated[dict, Depends(get_current_operator)],
    days: Annotated[int, Query(ge=1, le=366)] = 7,
) -> dict:
    """Token usage over the last ``days`` UTC days (today included).

    ``rows`` is one entry per account and model with request and token counts,
    largest first. ``daily`` is the per-day total, oldest first. Requests whose
    provider reported no usage are counted in ``requests_without_usage``.
    """
    return await UsageStore(store.redis).usage(days)


@router.get("/requests")
async def get_recent_requests(
    store: Annotated[RedisStore, Depends(get_store)],
    operator: Annotated[dict, Depends(get_current_operator)],
    limit: Annotated[int, Query(ge=1, le=1000)] = 100,
    account_id: Annotated[str | None, Query(max_length=128)] = None,
) -> dict:
    """Most recent requests first: outcome, status, latency, model, token counts."""
    data = await UsageStore(store.redis).recent(limit, account_id)
    return {"object": "list", "data": data}
