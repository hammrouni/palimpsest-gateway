"""OpenAI-compatible model listing: ``GET /v1/models`` and ``GET /v1/models/{id}``.

The model strings served by pool keys that are not suspended. Only the model
string is exposed; ``owned_by`` is fixed, never the upstream provider.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.api.deps import get_current_account, get_store
from app.errors import GatewayError
from app.pool.models import KeyState
from app.pool.registry import PoolRegistry
from app.store.redis_store import RedisStore

router = APIRouter(prefix="/v1/models", tags=["models"])

_OWNER = "palimpsest"


async def _served_models(store: RedisStore) -> list[dict]:
    """One OpenAI model object per distinct model string, sorted by id."""
    created: dict[str, int] = {}
    for record in await PoolRegistry(store.redis).list():
        if record.state == KeyState.SUSPENDED:
            continue
        stamp = int(record.created_at.timestamp())
        for model in record.models:
            created[model] = min(stamp, created.get(model, stamp))
    return [
        {"id": model, "object": "model", "created": created[model], "owned_by": _OWNER}
        for model in sorted(created)
    ]


@router.get("")
async def list_models(
    store: Annotated[RedisStore, Depends(get_store)],
    account: Annotated[dict, Depends(get_current_account)],
) -> dict:
    """Models the gateway can currently serve, in the OpenAI list format."""
    return {"object": "list", "data": await _served_models(store)}


@router.get("/{model_id:path}")
async def get_model(
    model_id: Annotated[str, Path()],
    store: Annotated[RedisStore, Depends(get_store)],
    account: Annotated[dict, Depends(get_current_account)],
) -> dict:
    """One model object, or 404 ``model_not_found`` if the pool does not serve it."""
    for model in await _served_models(store):
        if model["id"] == model_id:
            return model
    raise GatewayError(
        message=f"The model '{model_id}' does not exist.",
        status_code=404,
        error_type="invalid_request_error",
        code="model_not_found",
    )
