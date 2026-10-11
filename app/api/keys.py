"""API key management endpoints (list, create, revoke)."""
from typing import Annotated

from fastapi import APIRouter, Depends, Path, Request

from app.api.deps import (
    get_current_account,
    get_store,
    verify_account_ownership,
)
from app.api.schemas import KeyCreateResponse, KeyResponse
from app.errors import GatewayError
from app.store.redis_store import RedisStore

router = APIRouter(
    prefix="/v1/accounts/{account_id}/keys",
    tags=["keys"],
)


@router.get(
    "",
    response_model=list[KeyResponse],
)
async def list_keys(
    request: Request,
    account_id: Annotated[str, Path()],
    store: Annotated[RedisStore, Depends(get_store)],
    account: Annotated[dict, Depends(get_current_account)] = None,
) -> list[KeyResponse]:
    """List all keys for account (prefix only, no raw key/hash).

    Requires authentication. Ownership guard prevents cross-account access.

    Args:
        request: FastAPI request
        account_id: Account ID from path
        store: Redis store
        account: Authenticated account (dependency)

    Returns:
        200 with list of keys (id, prefix, created_at, last_used_at, status)
    """
    await verify_account_ownership(request, account_id)

    try:
        keys = await store.list_keys(account_id)
        return [
            KeyResponse(
                id=k["id"],
                prefix=k["prefix"],
                created_at=k["created_at"],
                last_used_at=k.get("last_used_at"),
                status=k["status"],
            )
            for k in keys
        ]
    except GatewayError:
        raise
    except Exception as exc:
        raise GatewayError(
            message="Failed to list keys",
            error_type="api_error",
            code="list_keys_failed",
            status_code=503,
        ) from exc


@router.post(
    "",
    response_model=KeyCreateResponse,
    status_code=201,
)
async def create_key(
    request: Request,
    account_id: Annotated[str, Path()],
    store: Annotated[RedisStore, Depends(get_store)],
    account: Annotated[dict, Depends(get_current_account)] = None,
) -> KeyCreateResponse:
    """Create an additional API key for account.

    Requires authentication. Raw key shown exactly once in 201 response.

    Args:
        request: FastAPI request
        account_id: Account ID from path
        store: Redis store
        account: Authenticated account (dependency)

    Returns:
        201 with key (raw key shown once)
    """
    await verify_account_ownership(request, account_id)

    try:
        key = await store._create_key(account_id)
        return KeyCreateResponse(
            id=key["id"],
            prefix=key["prefix"],
            key=key["key"],
            account_id=key["account_id"],
            created_at=key["created_at"],
        )
    except GatewayError:
        raise
    except Exception as exc:
        raise GatewayError(
            message="Failed to create key",
            error_type="api_error",
            code="key_creation_failed",
            status_code=503,
        ) from exc


@router.delete(
    "/{key_id}",
    status_code=204,
)
async def revoke_key(
    request: Request,
    account_id: Annotated[str, Path()],
    key_id: Annotated[str, Path()],
    store: Annotated[RedisStore, Depends(get_store)],
    account: Annotated[dict, Depends(get_current_account)] = None,
) -> None:
    """Revoke an API key immediately.

    Revocation takes effect immediately; next request on revoked key returns 401.
    Idempotent: revoking already-revoked key returns 204.

    Args:
        request: FastAPI request
        account_id: Account ID from path
        key_id: Key ID from path
        store: Redis store
        account: Authenticated account (dependency)

    Returns:
        204 No Content
    """
    await verify_account_ownership(request, account_id)

    try:
        await store.revoke_key(key_id, account_id)
    except GatewayError:
        raise
    except Exception as exc:
        raise GatewayError(
            message="Failed to revoke key",
            error_type="api_error",
            code="revoke_key_failed",
            status_code=503,
        ) from exc
