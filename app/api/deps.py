"""FastAPI dependencies (edge auth, store access)."""
from typing import Annotated

from fastapi import Depends, Header, Request

from app.core.crypto import hash_key
from app.errors import GatewayError
from app.store.redis_store import RedisStore


async def get_store(request: Request) -> RedisStore:
    """Get Redis store from request state."""
    if not hasattr(request.app.state, "store"):
        raise GatewayError(
            message="Store not initialized",
            status_code=500,
            code="store_not_initialized",
        )
    return request.app.state.store


async def get_current_account(
    request: Request,
    store: Annotated[RedisStore, Depends(get_store)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Authenticate a user-realm bearer key and return its account.

    Sets ``request.state.account_id``.

    Raises:
        GatewayError: 401 (invalid/missing/revoked) or 503 (store failure)
    """
    if not authorization:
        raise GatewayError(
            message="Missing API key",
            status_code=401,
            error_type="authentication_error",
            code="missing_api_key",
        )

    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise GatewayError(
            message="Invalid authorization header",
            status_code=401,
            error_type="authentication_error",
            code="invalid_api_key",
        )

    raw_key = parts[1]
    key_hash = hash_key(raw_key)

    try:
        account = await store.account_by_key_hash(key_hash)
        if not account:
            raise GatewayError(
                message="Invalid or missing API key",
                status_code=401,
                error_type="authentication_error",
                code="invalid_api_key",
            )

        request.state.account_id = account["id"]
        await store.update_key_last_used(account["key_id"])

        return account

    except GatewayError:
        raise
    except Exception as exc:
        raise GatewayError(
            message="Authentication failed",
            status_code=503,
            error_type="api_error",
            code="auth_failed",
        ) from exc


async def verify_account_ownership(
    request: Request,
    account_id: str,
) -> None:
    """Verify the path's account_id is the authenticated account.

    Args:
        request: FastAPI request
        account_id: Account ID from path

    Raises:
        GatewayError: 403 if mismatch
    """
    if not hasattr(request.state, "account_id"):
        raise GatewayError(
            message="Unauthorized",
            status_code=403,
            error_type="authentication_error",
            code="forbidden",
        )

    if request.state.account_id != account_id:
        raise GatewayError(
            message="Forbidden",
            status_code=403,
            error_type="authentication_error",
            code="forbidden",
        )
