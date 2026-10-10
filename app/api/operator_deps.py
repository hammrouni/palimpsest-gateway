"""Operator realm auth dependency (separate from user realm)."""
from typing import Annotated

from fastapi import Depends, Header, Request

from app.core.crypto import hash_key
from app.errors import GatewayError
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore


async def get_operator_store(request: Request) -> OperatorStore:
    """Get operator store from request state."""
    if not hasattr(request.app.state, "store"):
        raise GatewayError(
            message="Store not initialized",
            status_code=500,
            code="store_not_initialized",
        )
    return OperatorStore(request.app.state.store.redis)


async def get_user_store(request: Request) -> RedisStore:
    """The user-realm store, so a user key on an operator route can get a 403."""
    if not hasattr(request.app.state, "store"):
        raise GatewayError(
            message="Store not initialized",
            status_code=500,
            code="store_not_initialized",
        )
    return request.app.state.store


async def get_current_operator(
    request: Request,
    operator_store: Annotated[OperatorStore, Depends(get_operator_store)],
    user_store: Annotated[RedisStore, Depends(get_user_store)],
    authorization: Annotated[str | None, Header()] = None,
) -> dict[str, str]:
    """Authenticate an operator key and return ``{"id": ...}``.

    A user key here is 403 ``operator_access_denied``; a missing or unknown
    key is 401. A SHA-256 hash match is the authentication, so nothing is
    decrypted on this path. Sets ``request.state.operator_id``.

    Raises:
        GatewayError: 401 or 403
    """
    if not authorization:
        raise GatewayError(
            message="Missing API key",
            status_code=401,
            error_type="authentication_error",
            code="invalid_api_key",
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

    operator_record = await operator_store.get_by_hash(key_hash)
    if operator_record and operator_record.get("status") == "active":
        request.state.operator_id = operator_record["id"]
        await operator_store.touch_last_used(operator_record["id"])
        return {"id": operator_record["id"]}

    # Not in operator realm — a *valid* user-realm key is a distinct realm violation,
    # not a missing/invalid credential.
    account = await user_store.account_by_key_hash(key_hash)
    if account:
        raise GatewayError(
            message="Forbidden",
            status_code=403,
            error_type="authentication_error",
            code="operator_access_denied",
        )

    raise GatewayError(
        message="Invalid or missing API key",
        status_code=401,
        error_type="authentication_error",
        code="invalid_api_key",
    )
