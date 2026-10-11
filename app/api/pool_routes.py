"""Operator-realm Provider Key Pool endpoints, plus utilization and failover views.

Thin routes: writes go through ``PoolRegistry``. A user-realm key is rejected
with 403.
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Request
from pydantic import BaseModel, Field

from app.api.operator_deps import get_current_operator
from app.api.schemas import Error, PoolKeyRecordResponse
from app.audit import audit
from app.config import get_settings
from app.errors import GatewayError
from app.pool.catalog import CatalogError, fetch_models, key_catalog
from app.pool.events import recent_failover_events
from app.pool.limits import LiveLimitsError, get_observed, live_limits
from app.pool.models import KeyCreate, KeyUpdate, Provider
from app.pool.registry import DuplicateKeyError, KeyNotFoundError, PoolRegistry
from app.settlement import utilization_view

router = APIRouter(prefix="/v1/operator/pool/keys", tags=["pool"])
ops_router = APIRouter(prefix="/v1/operator/pool", tags=["pool"])

_ERROR_RESPONSES: dict[str, dict[str, Any]] = {
    "401": {"model": Error, "description": "Invalid or missing operator credential"},
    "403": {"model": Error, "description": "Valid user-realm key on an operator route"},
    "404": {"model": Error, "description": "Unknown key_id"},
    "422": {"model": Error, "description": "Invalid request"},
    "503": {"model": Error, "description": "Provider Key Pool store unavailable"},
}

_ADD_RESPONSES = {**_ERROR_RESPONSES, "409": {"model": Error, "description": "Duplicate key material"}}


async def get_pool_registry(request: Request) -> PoolRegistry:
    """Pool registry bound to the app's Redis client."""
    if not hasattr(request.app.state, "store"):
        raise GatewayError(
            message="Store not initialized",
            status_code=500,
            code="store_not_initialized",
        )
    return PoolRegistry(request.app.state.store.redis)


def _catalog_error(exc: Exception) -> GatewayError:
    return GatewayError(
        message=str(exc),
        status_code=502,
        error_type="upstream_error",
        code="provider_catalog_unavailable",
    )


async def _pool_call(fn: Any, /, *args: Any, **kwargs: Any) -> Any:
    """Run a registry call, mapping Redis failures to a 503.

    Other errors propagate; registry errors pass through for 404/409 mapping.
    """
    try:
        return await fn(*args, **kwargs)
    except (GatewayError, KeyNotFoundError, DuplicateKeyError):
        raise
    except Exception as exc:
        # redis-py errors derive from RedisError, not the builtins; recognise
        # them by module.
        is_transport = isinstance(exc, (ConnectionError, TimeoutError)) or (
            type(exc).__module__.split(".")[0] == "redis"
        )
        if not is_transport:
            raise
        raise GatewayError(
            message="Provider Key Pool store is unavailable.",
            status_code=503,
            error_type="api_error",
            code="pool_unavailable",
        ) from exc


def _invalid_request(param: str | None = None) -> GatewayError:
    return GatewayError(
        message="Invalid request.",
        status_code=422,
        error_type="invalid_request_error",
        code="invalid_request",
        param=param,
    )


def _key_not_found() -> GatewayError:
    return GatewayError(
        message="Key not found.",
        status_code=404,
        error_type="invalid_request_error",
        code="key_not_found",
    )


def _key_record_body(record: Any) -> dict:
    return {
        "object": "pool_key",
        "id": record.id,
        "provider": record.provider.value,
        "models": record.models,
        "key_hint": record.key_hint,
        "reset_at": record.reset_at.isoformat(),
        "next_reset_at": record.next_reset_at().isoformat(),
        "reset_period_hours": record.reset_period_hours,
        "daily_cap": record.daily_cap,
        "token_cap": record.token_cap,
        "label": record.label,
        "state": record.state.value,
        "operator_id": record.operator_id,
        "last_modified_by": record.last_modified_by,
        "created_at": record.created_at.isoformat(),
        "updated_at": record.updated_at.isoformat(),
    }


@router.post("", status_code=201, response_model=PoolKeyRecordResponse, responses=_ADD_RESPONSES)
async def add_key(
    body: KeyCreate,
    registry: Annotated[PoolRegistry, Depends(get_pool_registry)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> dict:
    """Add a Provider Key; routable immediately."""
    try:
        record = await _pool_call(registry.create, body, operator_id=operator["id"])
    except DuplicateKeyError as exc:
        raise GatewayError(
            message="A key with this material already exists.",
            status_code=409,
            error_type="invalid_request_error",
            code="duplicate_key",
        ) from exc
    await audit(registry.redis, operator["id"], "pool_key.add", record.id,
                {"provider": record.provider.value, "models": record.models})
    return _key_record_body(record)


@router.get("", response_model=list[PoolKeyRecordResponse], responses=_ERROR_RESPONSES)
async def list_keys(
    registry: Annotated[PoolRegistry, Depends(get_pool_registry)],
    operator: Annotated[dict, Depends(get_current_operator)],
    limit: int | None = None,
) -> list[dict]:
    """All keys in id order, with effective state (never key material)."""
    if limit is not None and not 1 <= limit <= 500:
        raise _invalid_request(param="limit")
    records = await _pool_call(registry.list)
    records.sort(key=lambda r: r.id)
    if limit is not None:
        records = records[:limit]
    return [_key_record_body(r) for r in records]


@router.get("/{key_id}", response_model=PoolKeyRecordResponse, responses=_ERROR_RESPONSES)
async def get_key(
    key_id: Annotated[str, Path()],
    registry: Annotated[PoolRegistry, Depends(get_pool_registry)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> dict:
    """Single record (never raw material); unknown id → 404."""
    record = await _pool_call(registry.get, key_id)
    if record is None:
        raise _key_not_found()
    return _key_record_body(record)


@router.patch("/{key_id}", response_model=PoolKeyRecordResponse, responses=_ERROR_RESPONSES)
async def update_key(
    key_id: Annotated[str, Path()],
    body: KeyUpdate,
    registry: Annotated[PoolRegistry, Depends(get_pool_registry)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> dict:
    """Edit a key's settings; applies from the next request.

    State, provider and key material are not touched.
    """
    try:
        record = await _pool_call(registry.update, key_id, body, operator_id=operator["id"])
    except KeyNotFoundError as exc:
        raise _key_not_found() from exc
    await audit(registry.redis, operator["id"], "pool_key.update", key_id,
                {"fields": sorted(body.model_dump(exclude_unset=True))})
    return _key_record_body(record)


@router.post("/{key_id}/suspend", response_model=PoolKeyRecordResponse, responses=_ERROR_RESPONSES)
async def suspend_key(
    key_id: Annotated[str, Path()],
    registry: Annotated[PoolRegistry, Depends(get_pool_registry)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> dict:
    """Immediate exclusion; idempotent (200 again if already suspended)."""
    try:
        record = await _pool_call(registry.suspend, key_id, operator_id=operator["id"])
    except KeyNotFoundError as exc:
        raise _key_not_found() from exc
    await audit(registry.redis, operator["id"], "pool_key.suspend", key_id)
    return _key_record_body(record)


@router.post("/{key_id}/resume", response_model=PoolKeyRecordResponse, responses=_ERROR_RESPONSES)
async def resume_key(
    key_id: Annotated[str, Path()],
    registry: Annotated[PoolRegistry, Depends(get_pool_registry)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> dict:
    """Clear the suspension flag only; idempotent."""
    try:
        record = await _pool_call(registry.resume, key_id, operator_id=operator["id"])
    except KeyNotFoundError as exc:
        raise _key_not_found() from exc
    await audit(registry.redis, operator["id"], "pool_key.resume", key_id)
    return _key_record_body(record)


@router.get("/{key_id}/provider-limits", responses=_ERROR_RESPONSES)
async def get_provider_limits(
    key_id: Annotated[str, Path()],
    registry: Annotated[PoolRegistry, Depends(get_pool_registry)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> dict:
    """What the provider says about this key's limits.

    ``observed`` is the rate-limit headers from the key's latest real request.
    ``live`` is the provider's own answer, fetched now (OpenRouter only); when the
    provider cannot be asked, ``live`` is null and ``live_error`` says why.
    Numbers and timestamps only: never key material or provider message text.
    """
    record = await _pool_call(registry.get, key_id)
    if record is None:
        raise _key_not_found()

    live = None
    live_error = None
    live_supported = record.provider == Provider.OPENROUTER
    if live_supported:
        try:
            live = await live_limits(registry, key_id, get_settings().openrouter_base_url)
        except KeyNotFoundError as exc:
            raise _key_not_found() from exc
        except LiveLimitsError as exc:
            live_error = str(exc)

    return {
        "key_id": key_id,
        "provider": record.provider.value,
        "live_supported": live_supported,
        "live": live,
        "live_error": live_error,
        "observed": await _pool_call(get_observed, registry.redis, key_id),
    }


@router.get("/{key_id}/available-models", responses=_ERROR_RESPONSES)
async def get_available_models(
    key_id: Annotated[str, Path()],
    registry: Annotated[PoolRegistry, Depends(get_pool_registry)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> dict:
    """The models this key's provider offers, for choosing which the key serves."""
    try:
        models = await _pool_call(key_catalog, registry, key_id)
    except KeyNotFoundError as exc:
        raise _key_not_found() from exc
    except CatalogError as exc:
        raise _catalog_error(exc) from exc
    if models is None:
        raise _key_not_found()
    return {"object": "list", "data": models}


@router.delete("/{key_id}", status_code=204, responses=_ERROR_RESPONSES)
async def remove_key(
    key_id: Annotated[str, Path()],
    registry: Annotated[PoolRegistry, Depends(get_pool_registry)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> None:
    """Remove a key and everything stored for it; a second delete is 404."""
    try:
        await _pool_call(registry.remove, key_id)
    except KeyNotFoundError as exc:
        raise _key_not_found() from exc
    await audit(registry.redis, operator["id"], "pool_key.remove", key_id)


# -- Read-only pool views -----------------------------------------------------


async def get_redis(request: Request):
    """The app's Redis client, for the read-only views."""
    if not hasattr(request.app.state, "store"):
        raise GatewayError(
            message="Store not initialized",
            status_code=500,
            code="store_not_initialized",
        )
    return request.app.state.store.redis


class AvailableModelsRequest(BaseModel):
    """A provider and a key to list that provider's models with. Used once, never stored."""

    provider: Provider
    key: str = Field(min_length=1, max_length=4096)


@ops_router.post("/available-models")
async def post_available_models(
    body: AvailableModelsRequest,
    operator: Annotated[dict, Depends(get_current_operator)],
) -> dict:
    """List a provider's models using a key that is not in the pool yet (the add-key form)."""
    try:
        models = await fetch_models(body.provider, body.key.strip())
    except CatalogError as exc:
        raise _catalog_error(exc) from exc
    return {"object": "list", "data": models}


@ops_router.get("/utilization")
async def get_utilization(
    redis: Annotated[object, Depends(get_redis)],
    operator: Annotated[dict, Depends(get_current_operator)],
):
    """Every key's state and capacity, plus pool-wide totals."""
    return await _pool_call(utilization_view, redis=redis)


@ops_router.get("/failover-events")
async def get_failover_events(
    redis: Annotated[object, Depends(get_redis)],
    operator: Annotated[dict, Depends(get_current_operator)],
    limit: int = 50,
    before: str | None = None,
):
    """Failover records, newest first, cursor-paginated by ``before``."""
    if limit < 1 or limit > 500:
        raise _invalid_request(param="limit")
    if before is not None:
        try:
            datetime.fromisoformat(before)
        except ValueError as exc:
            raise _invalid_request(param="before") from exc

    events, next_before = await _pool_call(recent_failover_events, limit, before, redis=redis)
    return {"events": events, "next_before": next_before}
