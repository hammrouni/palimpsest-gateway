"""Provider Key Pool models: providers, key states and records."""
from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from pydantic import BaseModel, Field, field_validator, model_validator


def window_bounds(
    reset_at: datetime, reset_period_hours: int, now: datetime
) -> tuple[float, float]:
    """A key's current quota window as unix ``(start, next_reset)``.

    ``reset_at + k * period`` for the largest integer ``k`` not after ``now``.
    ``k`` may be negative: a future ``reset_at`` ends the current window.
    """
    anchor = reset_at if reset_at.tzinfo is not None else reset_at.replace(tzinfo=UTC)
    period_seconds = (reset_period_hours if reset_period_hours > 0 else 24) * 3600
    elapsed = (now - anchor).total_seconds()
    k = math.floor(elapsed / period_seconds)
    window_start = anchor.timestamp() + k * period_seconds
    return window_start, window_start + period_seconds


class Provider(StrEnum):
    """Supported providers."""

    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    GOOGLE = "google"
    OPENROUTER = "openrouter"


class KeyState(StrEnum):
    """Per-key state."""

    ACTIVE = "active"
    EXHAUSTED = "exhausted"
    COOLDOWN = "cooldown"
    SUSPENDED = "suspended"


class KeyCreate(BaseModel):
    """Fields for adding a Provider Key; also the ``POST`` request body."""

    provider: Provider
    models: list[str] = Field(min_length=1)
    key: str
    reset_at: datetime | None = None
    reset_period_hours: int = Field(default=24, ge=1, le=720)
    daily_cap: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    # Tokens the key may serve per reset window, for the dashboard's budget view.
    token_cap: int | None = Field(default=None, ge=1)
    # A name the operator recognises ("Groq main"); shown instead of an id.
    label: str | None = Field(default=None, max_length=60)

    @field_validator("label")
    @classmethod
    def _clean_label(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None

    @field_validator("models")
    @classmethod
    def _non_blank_models(cls, value: list[str]) -> list[str]:
        cleaned = [m.strip() for m in value]
        if not all(cleaned):
            raise ValueError("models entries must not be blank")
        return list(dict.fromkeys(cleaned))  # drop repeats, keep order

    @field_validator("key")
    @classmethod
    def _non_blank_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("key must not be blank")
        return value

    @model_validator(mode="after")
    def _reset_at_not_in_past(self) -> KeyCreate:
        if self.reset_at is not None:
            if self.reset_at.tzinfo is None:
                raise ValueError("reset_at must include a timezone (UTC or explicit offset)")
            if self.reset_at <= datetime.now(UTC):
                raise ValueError("reset_at must be in the future")
        return self


class KeyUpdate(BaseModel):
    """Editable fields of an existing Provider Key.

    Only fields present in the request change; ``daily_cap: null`` removes the
    cap. Provider and key material are fixed at creation.
    """

    models: list[str] | None = Field(default=None, min_length=1)
    daily_cap: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    token_cap: int | None = Field(default=None, ge=1)
    label: str | None = Field(default=None, max_length=60)
    # The quota window the request and token caps follow.
    reset_at: datetime | None = None
    reset_period_hours: int | None = Field(default=None, ge=1, le=720)

    @field_validator("reset_at")
    @classmethod
    def _reset_at_has_a_timezone(cls, value: datetime | None) -> datetime | None:
        if value is not None and value.tzinfo is None:
            raise ValueError("reset_at must include a timezone (UTC or explicit offset)")
        return value

    @field_validator("label")
    @classmethod
    def _clean_label(cls, value: str | None) -> str | None:
        return (value.strip() or None) if value is not None else None

    @field_validator("models")
    @classmethod
    def _clean_models(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned = [m.strip() for m in value]
        if not all(cleaned):
            raise ValueError("models entries must not be blank")
        return list(dict.fromkeys(cleaned))  # drop repeats, keep the operator's order

    @model_validator(mode="after")
    def _something_to_change(self) -> KeyUpdate:
        editable = {"models", "daily_cap", "token_cap", "label", "reset_at", "reset_period_hours"}
        if not self.model_fields_set & editable:
            raise ValueError("provide at least one field to change")
        for required in ("reset_at", "reset_period_hours"):
            if required in self.model_fields_set and getattr(self, required) is None:
                raise ValueError(f"{required} cannot be cleared")
        if "models" in self.model_fields_set and self.models is None:
            raise ValueError("models must be a non-empty list")
        return self


class KeyRecord(BaseModel):
    """Operator-facing key record; never includes key material."""

    id: str
    provider: Provider
    models: list[str]
    key_hint: str
    reset_at: datetime
    reset_period_hours: int
    daily_cap: float | None = None
    token_cap: int | None = None
    label: str | None = None
    state: KeyState
    operator_id: str | None = None
    last_modified_by: str | None = None
    created_at: datetime
    updated_at: datetime

    def next_reset_at(self, now: datetime | None = None) -> datetime:
        """The next quota reset, ``reset_at + k * period``, strictly after now."""
        now = now or datetime.now(UTC)
        anchor = self.reset_at if self.reset_at.tzinfo else self.reset_at.replace(tzinfo=UTC)
        if anchor > now:
            return anchor
        period = timedelta(hours=self.reset_period_hours if self.reset_period_hours > 0 else 24)
        elapsed_periods = (now - anchor) // period
        return anchor + (elapsed_periods + 1) * period


class KeyStateView(BaseModel):
    """Effective-state summary of one key."""

    id: str
    provider: Provider
    models: list[str]
    state: KeyState
    reset_at: datetime
    seconds_remaining: float | None = None
    # Per-model cooldowns on an otherwise active key:
    # ``[{"model": str, "seconds_remaining": float}]``.
    cooling_models: list[dict] = Field(default_factory=list)
