"""Client rendering of pool exhaustion and fatal provider failures.

No provider name, key identity or provider text reaches the output; the
``X-Palimpsest-*`` headers carry only the retry count and reset time.
"""
from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from app.errors import GatewayError

if TYPE_CHECKING:
    from app.router.classify import Classification


class PoolExhaustedError(Exception):
    """No routable key left, or the retry budget spent.

    The loop attaches its ``FailoverTrace`` as ``trace``.
    """

    def __init__(self, code: str, next_reset: datetime | None, *, retry_count: int = 0):
        self.code = code  # "pool_exhausted" | "budget_exhausted"
        self.next_reset = next_reset
        self.retry_count = retry_count
        self.trace: object | None = None
        super().__init__(code)


# A wait this short is shown as seconds; anything longer is a quota-window reset.
_SHORT_WAIT_SECONDS = 900


def _in_words(seconds: int) -> str:
    """"about 7 hours", "about 25 minutes": a wait long enough that seconds mean nothing."""
    if seconds < 3600:
        minutes = max(1, round(seconds / 60))
        return f"about {minutes} minute{'s' if minutes != 1 else ''}"
    hours = round(seconds / 3600)
    if hours < 48:
        return f"about {hours} hour{'s' if hours != 1 else ''}"
    days = round(seconds / 86400)
    return f"about {days} days"


def _wait_seconds(next_reset: datetime | None) -> int | None:
    """Whole seconds until ``next_reset`` (at least 1), or None if unknown or past."""
    if next_reset is None:
        return None
    seconds = math.ceil((next_reset.astimezone(UTC) - datetime.now(UTC)).total_seconds())
    return seconds if seconds > 0 else 1


def _reset_headers(next_reset: datetime | None, retry_count: int) -> dict[str, str]:
    headers = {"X-Palimpsest-Retry-Count": str(retry_count)}
    if next_reset is not None:
        headers["X-Palimpsest-Next-Reset"] = next_reset.astimezone(UTC).isoformat()
        wait = _wait_seconds(next_reset)
        if wait is not None:
            headers["Retry-After"] = str(wait)
    return headers


def render_exhausted(error: PoolExhaustedError, model: str) -> GatewayError:
    """Render a ``PoolExhaustedError`` with retry headers.

    ``pool_exhausted`` (no key has capacity right now) is a 429, so clients
    that back off on 429 wait for the reset. A spent retry budget is a 503.
    """
    if error.code == "pool_exhausted":
        wait = _wait_seconds(error.next_reset)
        if wait is not None and wait <= _SHORT_WAIT_SECONDS:
            message = (
                f"No active capacity available for model {model!r}. "
                f"Retry in about {wait} second{'s' if wait != 1 else ''}."
            )
        elif wait is not None:
            message = (
                f"No active capacity available for model {model!r}. "
                f"Retry in {_in_words(wait)}."
            )
        else:
            message = (
                f"No active capacity available for model {model!r}. "
                "Retry after the next key reset."
            )
        status_code = 429
    else:
        message = (
            f"Retry budget exhausted while seeking capacity for model {model!r}."
        )
        status_code = 503

    return GatewayError(
        message=message,
        status_code=status_code,
        error_type="pool_exhausted",
        code=error.code,
        headers=_reset_headers(error.next_reset, error.retry_count),
    )


def render_fatal(classification: Classification, retry_count: int) -> GatewayError:
    """Render a ``fatal``-classified failure using the classifier's own client fields."""
    error_type = (
        "invalid_request_error"
        if classification.client_code
        in ("invalid_request", "unsupported_media_type", "context_length_exceeded")
        else "upstream_error"
    )
    return GatewayError(
        message=classification.client_message,
        status_code=classification.client_status,
        error_type=error_type,
        code=classification.client_code,
        headers={"X-Palimpsest-Retry-Count": str(retry_count)},
    )
