"""Account management endpoints (create account, get account info)."""
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.api.deps import get_store
from app.api.operator_deps import get_current_operator, get_operator_store
from app.api.schemas import (
    AccountResponse,
    CreateAccountRequest,
    KeyCreateResponse,
)
from app.config import get_settings
from app.errors import GatewayError
from app.store.redis_store import RedisStore

router = APIRouter(prefix="/v1/accounts", tags=["accounts"])


@router.post(
    "",
    response_model=AccountResponse,
    status_code=201,
)
async def create_account(
    request: Request,
    store: Annotated[RedisStore, Depends(get_store)],
    body: CreateAccountRequest,
    authorization: Annotated[str | None, Header()] = None,
) -> AccountResponse:
    """Create a new account and issue its first API key.

    Unauthenticated self-service signup while ``PALIMPSEST_ALLOW_SIGNUP`` is on
    (the default). With it off, only an operator key may create an account —
    a user key gets 403, no key 401. Returns account + raw key, shown exactly
    once in the 201 response; never retrieved afterwards.

    Args:
        request: FastAPI request
        store: Redis store
        body: Request body

    Returns:
        201 with account + first key (raw key shown once)
    """
    if not get_settings().allow_signup:
        await get_current_operator(
            request,
            await get_operator_store(request),
            store,
            authorization=authorization,
        )

    try:
        account = await store.create_account(body.display_name)
        return AccountResponse(
            id=account["id"],
            display_name=account["display_name"],
            created_at=account["created_at"],
            keys=[
                KeyCreateResponse(
                    id=k["id"],
                    prefix=k["prefix"],
                    key=k["key"],
                    account_id=k["account_id"],
                    created_at=k["created_at"],
                )
                for k in account["keys"]
            ],
        )
    except GatewayError:
        raise
    except Exception as exc:
        raise GatewayError(
            message="Failed to create account",
            error_type="api_error",
            code="account_creation_failed",
            status_code=503,
        ) from exc
