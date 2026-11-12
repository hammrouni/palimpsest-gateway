"""Operator live feed: what requests are doing right now.

Polled about once a second by the dashboard's Live Flow page. Each poll also
switches the feed on for a few seconds (see ``app.router.live``). Metadata only:
ids, models, status codes, counts and timings, never message content or keys.
"""
from __future__ import annotations

import time
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.api.deps import get_store
from app.api.operator_deps import get_current_operator
from app.router import live
from app.store.redis_store import RedisStore
from app.usage import calls

router = APIRouter(prefix="/operator/v1", tags=["operator"])

Store = Annotated[RedisStore, Depends(get_store)]
Operator = Annotated[dict, Depends(get_current_operator)]

# The id of an entry in the feed: milliseconds and a sequence number.
_CURSOR = r"^\d{1,20}-\d{1,20}$"


@router.get("/live")
async def live_events(
    store: Store,
    operator: Operator,
    after: Annotated[str | None, Query(pattern=_CURSOR)] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> dict:
    """Events after ``after`` (the newest ``limit`` when omitted), oldest first.

    Send back the returned ``cursor`` as ``after`` to get only what is new.
    """
    events, cursor = await live.read(store.redis, after, limit)
    return {"events": events, "cursor": cursor, "now": int(time.time() * 1000)}


@router.get("/calls")
async def call_history(
    store: Store,
    operator: Operator,
    limit: Annotated[int, Query(ge=1, le=1000)] = 500,
    account_id: Annotated[str | None, Query(max_length=128)] = None,
) -> dict:
    """The newest finished requests, newest first, each with every key and model it tried."""
    return {"object": "list", "data": await calls.recent(store.redis, limit, account_id)}
