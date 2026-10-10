"""Pydantic schemas for account/key endpoints (OpenAI-shaped)."""
from pydantic import BaseModel, Field


class ErrorDetail(BaseModel):
    """OpenAI error envelope."""

    message: str
    type: str
    code: str
    param: str | None = None


class Error(BaseModel):
    """OpenAI-shaped error response."""

    error: ErrorDetail


class KeyResponse(BaseModel):
    """API key response (no raw key or hash in list/detail)."""

    id: str
    prefix: str
    created_at: str
    last_used_at: str | None = None
    status: str = "active"


class KeyCreateResponse(BaseModel):
    """API key creation response (raw key shown exactly once)."""

    object: str = "api_key"
    id: str
    prefix: str
    key: str
    account_id: str
    created_at: str
    warning: str = "Store this key now; it will not be shown again."


class OperatorKeyResponse(BaseModel):
    """Operator key response (no raw key or hash in list/detail)."""

    id: str
    prefix: str
    created_at: str
    last_used_at: str | None = None
    status: str = "active"


class OperatorKeyCreateResponse(BaseModel):
    """Operator key creation response (raw key shown exactly once)."""

    object: str = "operator_key"
    id: str
    prefix: str
    key: str
    created_at: str
    warning: str = "Store this key now; it will not be shown again."


class PoolKeyRecordResponse(BaseModel):
    """Provider Key Pool record (operator realm) — never raw key material."""

    object: str = "pool_key"
    id: str
    provider: str
    models: list[str]
    key_hint: str
    reset_at: str
    next_reset_at: str
    reset_period_hours: int
    daily_cap: float | None = None
    token_cap: int | None = None
    label: str | None = None
    state: str
    operator_id: str | None = None
    last_modified_by: str | None = None
    created_at: str
    updated_at: str


class AccountResponse(BaseModel):
    """Account response with keys."""

    object: str = "account"
    id: str
    display_name: str
    created_at: str
    keys: list[KeyResponse] | list[KeyCreateResponse]


class AccountListItem(BaseModel):
    """Operator view of a user account — no key material.

    ``active_key_count`` counts keys that can still authenticate, so a revoked
    key stops inflating the number.
    """

    object: str = "account"
    id: str
    display_name: str
    created_at: str
    active_key_count: int
    status: str


class AccountStatusResponse(BaseModel):
    """Result of enabling or disabling an account."""

    object: str = "account"
    id: str
    status: str


class CreateAccountRequest(BaseModel):
    """Create account request."""

    display_name: str = Field(
        default="", max_length=200, description="Optional account name"
    )
