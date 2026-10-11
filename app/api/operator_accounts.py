"""Operator management of user accounts: list, keys, sessions, enable/disable, delete.

Account creation stays on ``POST /v1/accounts``.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field

from app.api.deps import get_store
from app.api.operator_deps import get_current_operator
from app.api.schemas import AccountListItem, AccountStatusResponse, KeyResponse
from app.audit import audit
from app.errors import GatewayError
from app.session.store import SessionStore
from app.settlement import forget_account, override_daily_cap
from app.store.redis_store import RedisStore

router = APIRouter(prefix="/operator/v1/accounts", tags=["operator"])


def _not_found(account_id: str) -> GatewayError:
    return GatewayError(
        message="Account not found.",
        status_code=404,
        error_type="invalid_request_error",
        code="account_not_found",
    )


async def _set_status(
    account_id: str, status: str, store: RedisStore, operator_id: str
) -> AccountStatusResponse:
    account = await store.set_account_status(account_id, status)
    if account is None:
        raise _not_found(account_id)
    await audit(store.redis, operator_id, "account.disable" if status == "disabled" else "account.enable", account_id)
    return AccountStatusResponse(id=account["id"], status=account["status"])


@router.get("", response_model=list[AccountListItem])
async def list_accounts(
    store: Annotated[RedisStore, Depends(get_store)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> list[AccountListItem]:
    """List user accounts with their active key count and enabled state."""
    return [
        AccountListItem(
            id=account["id"],
            display_name=account.get("display_name") or "",
            created_at=account.get("created_at") or "",
            active_key_count=account["active_key_count"],
            status=account.get("status") or "unknown",
        )
        for account in await store.list_all_accounts()
    ]


@router.delete("/{account_id}", status_code=204)
async def delete_account(
    account_id: Annotated[str, Path()],
    store: Annotated[RedisStore, Depends(get_store)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> None:
    """Permanently delete an account: its API keys, sessions and quota counters.

    Irreversible, and its keys stop authenticating at once. Historical usage
    statistics are aggregates and are kept. To block an account without losing
    anything, disable it instead.
    """
    if not await store.delete_account(account_id):
        raise _not_found(account_id)
    await SessionStore(store.redis).delete_all_sessions(account_id)
    await forget_account(account_id, redis=store.redis)
    await audit(store.redis, operator["id"], "account.delete", account_id)


@router.get("/{account_id}/sessions")
async def list_account_sessions(
    account_id: Annotated[str, Path()],
    store: Annotated[RedisStore, Depends(get_store)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> dict:
    """One account's live sessions, newest first: ids, timestamps, turn and token
    counts. Metadata only; conversation content is never exposed here."""
    if await store.get_account(account_id) is None:
        raise _not_found(account_id)
    sessions = await SessionStore(store.redis).list_sessions(account_id)
    return {"object": "list", "data": [{"object": "session", **s} for s in sessions]}


@router.get("/{account_id}/keys", response_model=list[KeyResponse])
async def list_account_keys(
    account_id: Annotated[str, Path()],
    store: Annotated[RedisStore, Depends(get_store)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> list[KeyResponse]:
    """List one account's keys — prefix only, never the key or its hash."""
    if await store.get_account(account_id) is None:
        raise _not_found(account_id)

    return [
        KeyResponse(
            id=key["id"],
            prefix=key["prefix"],
            created_at=key["created_at"],
            last_used_at=key.get("last_used_at"),
            status=key["status"],
        )
        for key in await store.list_keys(account_id)
    ]


@router.post("/{account_id}/disable", response_model=AccountStatusResponse)
async def disable_account(
    account_id: Annotated[str, Path()],
    store: Annotated[RedisStore, Depends(get_store)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> AccountStatusResponse:
    """Disable an account; its keys stop authenticating on the next request (401).

    Idempotent — disabling an already-disabled account is a no-op success, so a
    retried request never needs a state check first.
    """
    return await _set_status(account_id, "disabled", store, operator["id"])


@router.post("/{account_id}/enable", response_model=AccountStatusResponse)
async def enable_account(
    account_id: Annotated[str, Path()],
    store: Annotated[RedisStore, Depends(get_store)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> AccountStatusResponse:
    """Clear an account's disabled flag; its keys authenticate again."""
    return await _set_status(account_id, "active", store, operator["id"])


class DailyCapOverride(BaseModel):
    """Body of a per-account daily cap override."""

    daily_cap: int = Field(ge=1, le=10_000_000)


@router.post("/{account_id}/daily-cap")
async def set_daily_cap(
    account_id: Annotated[str, Path()],
    body: DailyCapOverride,
    store: Annotated[RedisStore, Depends(get_store)],
    operator: Annotated[dict, Depends(get_current_operator)],
) -> dict:
    """Set one account's request cap for the rest of today (UTC).

    The override lapses at the next UTC midnight and the account returns to the
    configured default. Refused with 409 while the pool is at or below its
    low-water mark: an override is never a way around the pool throttle.
    """
    if await store.get_account(account_id) is None:
        raise _not_found(account_id)
    expires_at = await override_daily_cap(account_id, body.daily_cap, redis=store.redis)
    if expires_at is None:
        raise GatewayError(
            message="Pool capacity is low; cap overrides are paused.",
            status_code=409,
            error_type="invalid_request_error",
            code="pool_low_water",
        )
    await audit(store.redis, operator["id"], "account.daily_cap", account_id, {"daily_cap": body.daily_cap})
    return {
        "object": "daily_cap_override",
        "account_id": account_id,
        "daily_cap": body.daily_cap,
        "expires_at": expires_at.isoformat(),
    }
