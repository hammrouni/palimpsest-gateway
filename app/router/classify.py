"""Provider failure classification: a pure function, no I/O.

``classify()`` is the only place that inspects a provider error to decide the
key action and the client response. The failover loop acts on its result.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from app.errors import PROVIDER_UNAVAILABLE, RATE_LIMIT

if TYPE_CHECKING:
    from app.provider.adapter import ProviderAttemptError

_QUOTA_MARKER_RE = re.compile(r"(?i)(quota|rate\s?limit|insufficient|credit|billing|balance)")

# A 429 that no wait fixes: the account is out of money, not briefly throttled.
_HARD_QUOTA_RE = re.compile(
    r"(?i)(no credits remaining|insufficient[_\s]?(credits?|quota|funds)|"
    r"exceeded your current quota|out of credits|credit balance is too low)"
)

# Groq/OpenAI-style "Request too large ... Limit 8000, Requested 14658": this one
# request can never fit, so every key would refuse it and none is unhealthy.
_REQUEST_TOO_LARGE_RE = re.compile(r"(?i)request too large")

_IMAGE_MARKER_RE = re.compile(
    r"(?i)(image|vision|visual|photo|picture|\.png|\.jpg|\.jpeg|\.gif|\.webp"
    r"|base64|multimodal|image_url|cannot read|does not support image)"
)

_CONTEXT_MARKER_RE = re.compile(
    r"(?i)(context[\s_-]?(length|window)|maximum context|too many tokens|prompt is too long|input length \d+ exceeds the maximum allowed input length|"
    r"input length \d+ exceeds \d+|maxi[mn]um.*context.*exceeded|context.*exceeded.*tokens?|too many tokens)"
)

_MODEL_SCOPED_403_RE = re.compile(
    r"(?i)(only available on|model.*not.*supported|not available for this model|model.*not.*found|"
    r"access.*denied.*model|model.*quota|per.*day.*limit|not allowed for this model|"
    r"model.*not.*available|capability.*not.*enabled|not supported by this key)"
)

_CONTEXT_EXCEEDED_MESSAGE = (
    "The conversation is too long for the selected model's context window; "
    "start a new session or choose a model with a larger context."
)
_UPSTREAM_REJECTED_MESSAGE = "No upstream credential accepted the request."
_INVALID_REQUEST_MESSAGE = (
    "Request was rejected by the upstream; no provider details are disclosed."
)
_REQUEST_TOO_LARGE_MESSAGE = (
    "The request is larger than the selected model's per-request token limit; "
    "shorten the conversation or choose a model with a higher limit."
)
_IMAGE_NOT_SUPPORTED_MESSAGE = (
    "The selected model does not support image input; remove image attachments "
    "or choose a vision-capable model."
)
_MODEL_SCOPED_403_MESSAGE = (
    "The selected model is not available with the current provider key; "
    "choose a different model or contact the operator."
)


@dataclass(frozen=True)
class Classification:
    """The disposition for one provider failure.

    The ``client_*`` fields are rendered only for ``fatal``; other states make
    the loop rotate, and exhaustion has its own messages.
    """

    state: str  # "transient" | "exhausted" | "rejected" | "fatal" | "model_scoped"
    client_status: int
    client_code: str
    client_message: str


def classify(error: ProviderAttemptError) -> Classification:
    """Map one provider failure to its disposition.

    401/403 mean the pool key itself was refused, not the client's request:
    ``rejected`` takes the key out of rotation and tries the next one.
    But model-scoped 403s (e.g., "only available on agentic harnesses") are
    not auth failures and should be a per-model cooldown, not a key suspension.
    """
    status = error.status_code
    message = error.message or ""
    code = error.code or ""

    if status == 429 and _REQUEST_TOO_LARGE_RE.search(message):
        return Classification(
            "fatal", 400, "context_length_exceeded", _REQUEST_TOO_LARGE_MESSAGE
        )
    if status == 429 and _HARD_QUOTA_RE.search(message):
        return Classification("exhausted", 429, "rate_limit_exceeded", RATE_LIMIT)
    if status == 429:
        return Classification("transient", 503, "provider_unavailable", PROVIDER_UNAVAILABLE)
    if status == 402:
        return Classification("exhausted", 429, "rate_limit_exceeded", RATE_LIMIT)
    if (status == 400 or status == 403) and _QUOTA_MARKER_RE.search(message):
        return Classification("exhausted", 429, "rate_limit_exceeded", RATE_LIMIT)
    if status == 400 and _CONTEXT_MARKER_RE.search(message):
        # Prompt size vs. the model's window: actionable, leaks no credentials.
        return Classification(
            "fatal", 400, "context_length_exceeded", _CONTEXT_EXCEEDED_MESSAGE
        )
    if code == "context_length_exceeded":
        # adapter.py maps ContextWindowExceededError to this code
        return Classification(
            "fatal", 400, "context_length_exceeded", _CONTEXT_EXCEEDED_MESSAGE
        )
    if status == 400 and _IMAGE_MARKER_RE.search(message):
        # A capability mismatch, not a credential secret: the client can fix
        # this by dropping images or picking a vision model. Safe to surface.
        return Classification(
            "fatal", 400, "unsupported_media_type", _IMAGE_NOT_SUPPORTED_MESSAGE
        )
    # 408 (a timeout) is retried like a 5xx: the next key may be quicker.
    if status is None or status == 408 or 500 <= status <= 599:
        return Classification("transient", 503, "provider_unavailable", PROVIDER_UNAVAILABLE)
    if status == 404:
        # Model not found: exclude this model on this key for a long TTL
        return Classification("model_scoped", 502, "model_not_found", _MODEL_SCOPED_403_MESSAGE)
    if status in (401, 403):
        # Check if this is a model-scoped 403 (not a real auth failure)
        if _MODEL_SCOPED_403_RE.search(message):
            return Classification("model_scoped", 502, "model_not_allowed", _MODEL_SCOPED_403_MESSAGE)
        # Real auth failure: suspend the entire key
        return Classification("rejected", 502, "upstream_rejected", _UPSTREAM_REJECTED_MESSAGE)
    if status == 400:
        return Classification("fatal", 400, "invalid_request", _INVALID_REQUEST_MESSAGE)
    return Classification("fatal", 502, "upstream_rejected", _UPSTREAM_REJECTED_MESSAGE)
