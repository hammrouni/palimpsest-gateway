"""Gateway configuration from ``PALIMPSEST_*`` environment variables.

Provider keys and model strings are never echoed to clients. Every setting has
a default, so the gateway boots with only the secrets set.
"""
from __future__ import annotations

import json
import logging
from functools import lru_cache

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    model_validator,
)
from pydantic_settings import BaseSettings

# Settings an operator may read and change at runtime. Anything else is
# infrastructure or a credential and is neither returned nor writable.
TUNABLE_FIELDS: tuple[str, ...] = (
    "verbatim_turns_n",
    "retry_budget",
    "pool_low_water_mark",
    "user_daily_cap",
    "request_log_prompts",
    "cooldown_seconds",
    "failover_event_log_size",
    "request_log_ttl_hours",
)


class Settings(BaseSettings):
    """Runtime configuration for the gateway."""

    project_name: str = "palimpsest-gateway"
    project_version: str = "2.1.0"

    # Single-key shortcut: on startup PROVIDER_API_KEY is added to the pool for
    # PROVIDER_NAME, serving PROVIDER_MODEL. Further keys go through the pool API.
    provider_name: str = "openai"
    provider_model: str = "gpt-4o-mini"
    provider_api_key: str = ""
    # Per-attempt provider timeout, in seconds.
    provider_timeout: float = Field(default=60.0, gt=0)

    # How often each instance re-reads operator overrides from Redis. 0 disables.
    config_refresh_seconds: float = Field(default=5.0, ge=0)

    redis_url: str = "redis://localhost:6379/0"

    # CORS origins as a JSON array string, e.g. '["http://localhost:3000"]'.
    # Empty by default: the dashboard is same-origin and non-browser clients
    # ignore CORS.
    cors_origins: str = "[]"

    # Self-service signup on POST /v1/accounts. When off, account creation
    # needs an operator key.
    allow_signup: bool = True

    # Maximum length of a client-supplied X-Session-ID.
    max_session_id_length: int = Field(default=128, ge=1, le=512)

    # Create a session for an unknown X-Session-ID instead of returning 404.
    # Sessions stay per-account either way.
    auto_create_sessions: bool = False

    # Operator statistics: days of token counters kept, recent requests kept.
    usage_retention_days: int = Field(default=90, ge=1, le=365)
    usage_recent_max: int = Field(default=500, ge=10, le=10000)

    # Session lock contention: wait up to WAIT_MS (polling every POLL_MS), then 423.
    session_lock_wait_ms: int = 2000
    session_lock_poll_ms: int = 100

    # Stored turns re-sent verbatim to a replacement provider on failover.
    verbatim_turns_n: int = Field(default=4, ge=1)

    # Log the current turn's messages. Stored history is never logged.
    request_log_prompts: bool = False

    # Default pause after a transient provider failure, when the provider gives none.
    cooldown_seconds: int = Field(default=60, ge=1)

    # Retries after the first attempt: at most retry_budget + 1 provider calls.
    # 0 (no failover) is env-only; the runtime PATCH requires >= 1.
    retry_budget: int = Field(default=3, ge=0, le=20)

    # Lifetime of the per-request settlement event.
    request_log_ttl_hours: int = Field(default=24, ge=1)

    # Throttle new requests when capped pool capacity falls to this. 0 = off.
    pool_low_water_mark: int = Field(default=0, ge=0)

    # Maximum length of the failover-events list.
    failover_event_log_size: int = Field(default=1000, ge=1)

    # Settlement idempotency marker TTL. Must outlive the longest request
    # (at least provider_timeout * retry_budget).
    settle_id_ttl_seconds: int = Field(default=300, ge=1)

    # Default per-account daily request cap. None = uncapped.
    user_daily_cap: int | None = Field(default=None, ge=0)

    # Provider base URLs.
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    openai_base_url: str | None = None

    @property
    def cors_origin_list(self) -> list[str]:
        """``cors_origins`` as a list; a malformed value means no origins."""
        try:
            parsed = json.loads(self.cors_origins)
        except (ValueError, TypeError):
            return []
        if isinstance(parsed, list):
            return [str(origin) for origin in parsed]
        if isinstance(parsed, str) and parsed:
            return [parsed]
        return []

    model_config = {
        "env_prefix": "PALIMPSEST_",
        "env_file": ".env",
        "env_file_encoding": "utf-8",
        "extra": "ignore",
    }


@lru_cache
def get_settings() -> Settings:
    """Return the cached settings instance."""
    return Settings()


def tunable_values() -> dict[str, object]:
    """Current values of the operator-tunable settings, and nothing else."""
    settings = get_settings()
    return {field: getattr(settings, field) for field in TUNABLE_FIELDS}


def apply_tunables(values: dict[str, object]) -> dict[str, object]:
    """Assign overrides onto the cached ``Settings`` instance in place.

    Clearing the cache would rebuild from the environment and lose the
    override. Values must already have passed ``ConfigPatch`` validation.
    """
    settings = get_settings()
    for field, value in values.items():
        setattr(settings, field, value)
    return values


def apply_persisted_overrides(overrides: dict[str, object]) -> dict[str, object]:
    """Re-apply persisted operator overrides; returns those actually applied.

    Unknown or non-tunable names are skipped, and every value is revalidated.
    """
    applicable: dict[str, object] = {}
    for field, value in overrides.items():
        if field not in TUNABLE_FIELDS or field not in Settings.model_fields:
            continue
        try:
            validated = ConfigPatch(**{field: value}).model_dump(exclude_unset=True)
        except ValidationError:
            logging.getLogger("palimpsest.config").warning(
                "ignoring invalid persisted override for %s", field
            )
            continue
        applicable[field] = validated[field]
    return apply_tunables(applicable)


class ConfigPatch(BaseModel):
    """A partial update to the tunable settings, with each setting's bounds.

    ``extra="allow"`` keeps unknown keys visible so the route can tell an
    ignored unknown key from a known but non-tunable one.
    """

    model_config = ConfigDict(extra="allow")

    verbatim_turns_n: int | None = Field(default=None, ge=1)
    retry_budget: int | None = Field(default=None, ge=1, le=20)
    pool_low_water_mark: int | None = Field(default=None, ge=0)
    user_daily_cap: int | None = Field(default=None, ge=1)
    request_log_prompts: bool | None = None
    cooldown_seconds: int | None = Field(default=None, ge=1)
    failover_event_log_size: int | None = Field(default=None, ge=1, le=1_000_000)
    request_log_ttl_hours: int | None = Field(default=None, ge=1, le=8760)

    @model_validator(mode="after")
    def _null_only_clears_the_user_cap(self) -> ConfigPatch:
        """``null`` clears ``user_daily_cap``; every other setting requires a value."""
        for field in self.model_fields_set:
            if field in TUNABLE_FIELDS and field != "user_daily_cap" and getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self
