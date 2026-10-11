"""User-realm session endpoints: list your sessions, delete one with its history.

Every lookup is scoped to the authenticated account, so another account's
session is simply not found.
"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Path

from app.api.chat import get_session_store, valid_session_id
from app.api.deps import get_current_account
from app.errors import GatewayError
from app.session.store import SessionStore

router = APIRouter(prefix="/v1/sessions", tags=["sessions"])


@router.get("")
async def list_sessions(
    session_store: Annotated[SessionStore, Depends(get_session_store)],
    account: Annotated[dict, Depends(get_current_account)],
) -> dict:
    """The caller's live sessions, most recently active first (metadata only)."""
    sessions = await session_store.list_sessions(account["id"])
    return {"object": "list", "data": [{"object": "session", **s} for s in sessions]}


@router.delete("/{session_id}", status_code=204)
async def delete_session(
    session_id: Annotated[str, Path()],
    session_store: Annotated[SessionStore, Depends(get_session_store)],
    account: Annotated[dict, Depends(get_current_account)],
) -> None:
    """Delete one of the caller's sessions and its stored history, permanently."""
    if not valid_session_id(session_id) or not await session_store.delete_session(
        account["id"], session_id
    ):
        raise GatewayError(
            message="Session not found.",
            status_code=404,
            error_type="invalid_request_error",
            code="session_not_found",
        )
