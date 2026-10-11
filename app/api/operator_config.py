"""Operator runtime configuration: read and change the tunable settings.

Validation lives in ``ConfigPatch``; its failures surface as 422 with ``param``.
"""
from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request

from app.api.operator_deps import get_current_operator
from app.audit import audit
from app.config import (
    TUNABLE_FIELDS,
    ConfigPatch,
    Settings,
    apply_tunables,
    tunable_values,
)
from app.errors import GatewayError
from app.store.config import ConfigStore
from app.store.redis_store import RedisStore

router = APIRouter(prefix="/operator/v1/config", tags=["operator"])


def _config_store(request: Request) -> ConfigStore:
    """Build the override store from the request's Redis client.

    Args:
        request: Incoming request.

    Returns:
        A ConfigStore over the app's Redis connection.
    """
    store: RedisStore = request.app.state.store
    return ConfigStore(store.redis)


@router.get("")
async def read_config(
    operator: Annotated[dict, Depends(get_current_operator)],
) -> dict[str, Any]:
    """Return the operator-tunable settings — never credentials or infrastructure."""
    return tunable_values()


@router.patch("")
async def update_config(
    request: Request,
    patch: ConfigPatch,
    operator: Annotated[dict, Depends(get_current_operator)],
) -> dict[str, Any]:
    """Apply the supplied changes and persist them across a restart.

    A key that names a real setting but is not tunable is refused rather than
    dropped: silently ignoring an attempt to change ``redis_url`` would leave an
    operator believing infrastructure had been reconfigured.
    """
    supplied = patch.model_dump(exclude_unset=True)

    refused = sorted(key for key in supplied if key in Settings.model_fields and key not in TUNABLE_FIELDS)
    if refused:
        raise GatewayError(
            message=f"Not tunable at runtime: {', '.join(refused)}.",
            status_code=422,
            error_type="invalid_request_error",
            code="not_tunable",
            param=refused[0],
        )

    changes = {key: value for key, value in supplied.items() if key in TUNABLE_FIELDS}
    if changes:
        # Audit "old" is what was actually in effect (env default or earlier
        # override), not just what happens to be in the overrides hash.
        previous = tunable_values()
        # Persist first: if Redis fails the request fails and nothing has changed in
        # memory, instead of a live setting that silently vanishes on restart.
        await _config_store(request).set_overrides(
            changes, operator_id=operator["id"], previous=previous
        )
        apply_tunables(changes)
        await audit(request.app.state.store.redis, operator["id"], "config.update", None,
                    {k: {"old": previous.get(k), "new": v} for k, v in changes.items()})

    return tunable_values()
