"""Gateway errors and the OpenAI-compatible error envelope.

The message constants below are the provider-facing strings the failure
classifier renders. The real provider, the provider's own error text, stack
traces and provider keys are never echoed to a client (non-disclosure).
"""
from __future__ import annotations

# --- Non-disclosing, OpenAI-compatible error messages -----------------------
RATE_LIMIT = "Rate limit exceeded."
PROVIDER_UNAVAILABLE = "The provider is temporarily unavailable."


class GatewayError(Exception):
    """Application error rendered as ``{"error": {...}}`` by the registered handler."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int = 500,
        error_type: str = "api_error",
        code: str = "internal_error",
        param: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message
        self.status_code = status_code
        self.type = error_type
        self.code = code
        self.param = param
        self.headers = headers
        super().__init__(message)
