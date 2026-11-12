"""Provider Key Pool models: providers, key states and records."""
from __future__ import annotations

import ipaddress
import math
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from app.config import get_settings


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
    MISTRAL = "mistral"
    GROQ = "groq"
    ZAI = "zai"
    XAI = "xai"
    GITHUB = "github"
    CUSTOM = "custom"


# Providers whose endpoint an operator may point at another host, per key: the
# OpenAI-compatible ones. Anthropic, Google and OpenRouter keep their own hosts.
BASE_URL_PROVIDERS = frozenset(
    {
        Provider.OPENAI,
        Provider.MISTRAL,
        Provider.GROQ,
        Provider.ZAI,
        Provider.XAI,
        Provider.GITHUB,
        Provider.CUSTOM,
    }
)

# Where a provider is reached when no base URL is set, for the model catalogue
# and the dashboard hint. ``None``: the provider library knows it.
DEFAULT_BASE_URLS: dict[Provider, str | None] = {
    Provider.OPENAI: None,
    Provider.MISTRAL: None,
    Provider.GROQ: None,
    Provider.ZAI: "https://api.z.ai/api/paas/v4",
    Provider.XAI: "https://api.x.ai/v1",
    Provider.GITHUB: "https://models.github.ai/inference",
}

_BASE_URL_MAX = 300
# Cloud instance-metadata services: never a model endpoint, and reachable from
# inside most clouds, so a provider key must not be sent to them.
_BLOCKED_HOSTS = frozenset({"metadata.google.internal", "metadata", "instance-data"})
_AWS_IPV6_METADATA = ipaddress.ip_network("fd00:ec2::/32")


def clean_base_url(value: str) -> str:
    """A provider base URL, normalised: ``http(s)://host[:port][/path]``, no trailing slash.

    Self-hosted servers (Ollama, vLLM, LM Studio) live on private addresses and
    plain http, so those are allowed. Embedded credentials, query strings,
    fragments and cloud-metadata addresses are not: the provider key is sent to
    this URL, so it must name a model server and nothing else.
    """
    value = value.strip()
    if not value or len(value) > _BASE_URL_MAX:
        raise ValueError("base_url must be a URL of at most 300 characters")
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("base_url must start with http:// or https:// and name a host")
    if parts.username is not None or parts.password is not None:
        raise ValueError("base_url must not contain credentials")
    if parts.query or parts.fragment:
        raise ValueError("base_url must not contain a query string or fragment")
    try:
        parts.port  # noqa: B018 - raises ValueError for a malformed port
    except ValueError as exc:
        raise ValueError("base_url has an invalid port") from exc
    host = parts.hostname.lower()
    if host in _BLOCKED_HOSTS:
        raise ValueError("base_url must not point at a cloud metadata service")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        address = None
    if address is not None and (
        address.is_link_local
        or address.is_unspecified
        or address.is_multicast
        or (address.version == 6 and address in _AWS_IPV6_METADATA)
    ):
        raise ValueError("base_url must not point at a link-local or metadata address")
    return value.rstrip("/")


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
    # Where this key's requests go, for OpenAI-compatible providers. Required for
    # ``custom`` unless PALIMPSEST_CUSTOM_BASE_URL is set; otherwise the
    # provider's own endpoint is used.
    base_url: str | None = None

    @field_validator("base_url")
    @classmethod
    def _clean_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return clean_base_url(value)

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
    def _base_url_fits_provider(self) -> KeyCreate:
        if self.base_url is not None and self.provider not in BASE_URL_PROVIDERS:
            raise ValueError(f"{self.provider.value} keys do not take a base_url")
        if (
            self.provider == Provider.CUSTOM
            and self.base_url is None
            and not get_settings().custom_base_url
        ):
            raise ValueError("a custom provider needs a base_url")
        return self

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
    # Where this key's requests go; ``null`` goes back to the provider's endpoint.
    base_url: str | None = None

    @field_validator("base_url")
    @classmethod
    def _clean_base_url(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        return clean_base_url(value)

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
        editable = {
            "models", "daily_cap", "token_cap", "label", "reset_at", "reset_period_hours",
            "base_url",
        }
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
    base_url: str | None = None
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
    # Why the key is held back (see ``classify.pause_reason``), if known.
    reason: str | None = None
    # Per-model cooldowns on an otherwise active key:
    # ``[{"model": str, "seconds_remaining": float, "reason": str | None}]``.
    cooling_models: list[dict] = Field(default_factory=list)
