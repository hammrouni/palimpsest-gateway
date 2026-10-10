"""Single-attempt LiteLLM provider adapter; the only caller of key decryption.

One call is one attempt against one key. Retrying belongs to the failover
loop; LiteLLM's own retry machinery is never used.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import litellm
from litellm import acompletion
from litellm.exceptions import (
    APIError,
    BadRequestError,
    ContextWindowExceededError,
    InternalServerError,
    RateLimitError,
    ServiceUnavailableError,
    Timeout,
)

from app.config import get_settings
from app.pool.limits import record_observed
from app.pool.models import Provider
from app.pool.registry import KeyNotFoundError, PoolRegistry

litellm.verbose = False
litellm.logging = False

logger = logging.getLogger("palimpsest.provider")

_MODEL_PREFIX: dict[Provider, str] = {
    Provider.OPENAI: "openai",
    Provider.ANTHROPIC: "anthropic",
    # litellm has no "google/" route; Gemini API keys are served under "gemini/".
    Provider.GOOGLE: "gemini",
    Provider.OPENROUTER: "openrouter",
}

_LITELLM_PARAM_WHITELIST = frozenset({
    "temperature", "max_tokens", "max_completion_tokens", "top_p",
    "frequency_penalty", "presence_penalty", "stop", "seed", "n", "stream",
    "tools", "tool_choice", "parallel_tool_calls",
})


class CandidateGoneError(KeyNotFoundError):
    """The selected key was removed between selection and the attempt.

    ``__str__`` omits ``key_id`` so it cannot leak through logged tracebacks.
    """

    def __init__(self, key_id: str):
        self.key_id = key_id
        super().__init__("candidate key no longer available")

    def __str__(self) -> str:
        return "candidate key no longer available"


class ProviderAttemptError(Exception):
    """One failed attempt; ``message`` is scrubbed of key material."""

    def __init__(
        self, status_code: int | None, code: str | None, message: str, provider: Provider
    ):
        self.status_code = status_code
        self.code = code
        self.message = message
        self.provider = provider
        super().__init__(message)


@dataclass
class AttemptResult:
    """Outcome of one ``attempt()`` call."""

    ok: bool
    response: Any = None
    error: ProviderAttemptError | None = None
    # Provider response headers (success or error), for rate-limit observation.
    headers: dict | None = None


def _mapped_model(provider: Provider, model: str) -> str:
    return f"{_MODEL_PREFIX[provider]}/{model}"


def _api_base(provider: Provider) -> str | None:
    settings = get_settings()
    if provider == Provider.OPENROUTER:
        return settings.openrouter_base_url
    if provider == Provider.OPENAI:
        return settings.openai_base_url or None
    return None


def _scrub(message: str, material: str) -> str:
    """Strip the raw key material out of a provider error message."""
    if not material:
        return message
    return message.replace(material, "[REDACTED]")


def _attempt_error(exc: Exception, code: str, material: str, provider: Provider) -> AttemptResult:
    status_code = getattr(exc, "status_code", None)
    message = _scrub(str(getattr(exc, "message", exc)), material)
    response = getattr(exc, "response", None)
    return AttemptResult(
        ok=False,
        error=ProviderAttemptError(status_code, code, message, provider),
        headers=dict(getattr(response, "headers", None) or {}),
    )


async def attempt(
    registry: PoolRegistry,
    key_id: str,
    model: str,
    messages: list[dict],
    **params: Any,
) -> AttemptResult:
    """One provider attempt, then remember the rate-limit headers it came back with."""
    result = await _attempt_once(registry, key_id, model, messages, **params)
    if result.headers:
        await record_observed(registry.redis, key_id, result.headers)
    return result


async def _attempt_once(
    registry: PoolRegistry,
    key_id: str,
    model: str,
    messages: list[dict],
    **params: Any,
) -> AttemptResult:
    """Exactly one ``litellm.acompletion`` call with ``key_id``'s decrypted key.

    Raises ``CandidateGoneError`` if the key was removed since selection. Any
    provider failure comes back as ``AttemptResult(ok=False, error=...)``.
    """
    record = await registry.get(key_id)
    if record is None:
        raise CandidateGoneError(key_id)

    try:
        material = await registry.get_key_material(key_id)
    except KeyNotFoundError as exc:
        raise CandidateGoneError(key_id) from exc

    provider = record.provider
    mapped_model = _mapped_model(provider, model)
    api_base = _api_base(provider)

    filtered_params = {
        k: v for k, v in params.items() if k in _LITELLM_PARAM_WHITELIST
    }
    if filtered_params.get("stream"):
        # Streams carry token counts only when asked; the usage-only chunk is
        # consumed by the gateway, not forwarded.
        filtered_params["stream_options"] = {"include_usage": True}
    unknown = set(params) - _LITELLM_PARAM_WHITELIST
    if unknown:
        logger.debug("dropping unrecognized litellm params: %s", unknown)

    try:
        response = await acompletion(
            model=mapped_model,
            api_key=material,
            api_base=api_base,
            messages=messages,
            # litellm's own default is 6000s, holding the session lock throughout.
            timeout=get_settings().provider_timeout,
            **filtered_params,
        )
        hidden = getattr(response, "_hidden_params", None) or {}
        return AttemptResult(
            ok=True, response=response, headers=hidden.get("additional_headers") or {}
        )
    except RateLimitError as exc:
        return _attempt_error(exc, "rate_limit", material, provider)
    except ContextWindowExceededError as exc:
        return _attempt_error(exc, "context_length_exceeded", material, provider)
    except BadRequestError as exc:
        return _attempt_error(exc, "bad_request", material, provider)
    except ServiceUnavailableError as exc:
        return _attempt_error(exc, "service_unavailable", material, provider)
    except InternalServerError as exc:
        return _attempt_error(exc, "internal_error", material, provider)
    except Timeout as exc:
        return _attempt_error(exc, "timeout", material, provider)
    except APIError as exc:
        return _attempt_error(exc, "provider_error", material, provider)
    except Exception as exc:  # nothing escapes the attempt
        # litellm's AuthenticationError/PermissionDeniedError/NotFoundError are not
        # APIError subclasses; keep their status so a revoked key is not read as
        # a transient outage.
        status_code = getattr(exc, "status_code", None)
        if not isinstance(status_code, int):
            status_code = None
        message = _scrub(str(exc), material)
        return AttemptResult(
            ok=False,
            error=ProviderAttemptError(status_code, "unexpected_error", message, provider),
        )
