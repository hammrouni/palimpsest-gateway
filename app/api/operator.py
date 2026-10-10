"""Operator key endpoints: issue, list, revoke."""
from __future__ import annotations

import re
from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.api.operator_deps import get_current_operator, get_operator_store
from app.api.schemas import OperatorKeyCreateResponse, OperatorKeyResponse
from app.audit import audit
from app.errors import GatewayError
from app.store.operator import LastActiveKeyError, OperatorStore

router = APIRouter(prefix="/operator/v1/keys", tags=["operator"])


@router.post("", response_model=OperatorKeyCreateResponse, status_code=201)
async def create_operator_key(
    store: Annotated[OperatorStore, Depends(get_operator_store)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> OperatorKeyCreateResponse:
    """Issue a new operator key; raw key returned exactly once."""
    key = await store.create_key()
    await audit(store.redis, operator["id"], "operator_key.create", key["id"])
    return OperatorKeyCreateResponse(
        id=key["id"],
        prefix=key["prefix"],
        key=key["key"],
        created_at=key["created_at"],
    )


@router.get("", response_model=list[OperatorKeyResponse])
async def list_operator_keys(
    store: Annotated[OperatorStore, Depends(get_operator_store)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> list[OperatorKeyResponse]:
    """List operator keys — never ``key_material``/``key_hash``."""
    keys = await store.list_keys()
    return [
        OperatorKeyResponse(
            id=k["id"],
            prefix=k["prefix"],
            created_at=k["created_at"],
            last_used_at=k.get("last_used_at"),
            status=k["status"],
        )
        for k in keys
    ]


@router.delete("/{key_id}", status_code=204)
async def revoke_operator_key(
    key_id: Annotated[str, Path()],
    store: Annotated[OperatorStore, Depends(get_operator_store)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> None:
    """Revoke an operator key; takes effect immediately (next use → 401)."""
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key_id):
        # Not an id we could have issued; also keeps "by_hash:..." style paths away
        # from record-key construction (WRONGTYPE -> 500).
        raise GatewayError(
            message="Key not found.",
            status_code=404,
            error_type="invalid_request_error",
            code="key_not_found",
        )
    try:
        revoked = await store.revoke_key(key_id)
    except LastActiveKeyError as err:
        raise GatewayError(
            message="Cannot revoke the last active operator key.",
            status_code=409,
            error_type="invalid_request_error",
            code="last_operator_key",
        ) from err
    if not revoked:
        raise GatewayError(
            message="Key not found.",
            status_code=404,
            error_type="invalid_request_error",
            code="key_not_found",
        )
    await audit(store.redis, operator["id"], "operator_key.revoke", key_id)
