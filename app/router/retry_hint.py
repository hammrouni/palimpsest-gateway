"""How long a provider says to wait before trying again.

A 429 usually carries the answer: a ``Retry-After`` header, or text such as Google's
"Please retry in 7h14m58.6s" and its ``"retryDelay": "37s"`` field. Pausing a key for
exactly that long beats a fixed guess: a per-minute limit comes back in seconds, a
daily quota in hours, and hammering the second kind every minute only wastes calls.

Also reads OpenRouter's ``x-ratelimit-reset`` (epoch-ms) and ``x-ratelimit-reset-after``
for daily quotas, which carry no ``Retry-After``.
"""
from __future__ import annotations

import math
import re
import time
from collections.abc import Mapping
from typing import Any

MAX_HINT_SECONDS = 86400  # a provider asking for more than a day is not trusted
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}

_DURATION = r"(?:\d+(?:\.\d+)?[dhms])+"
_RETRY_IN = re.compile(rf"retry in\s+({_DURATION})", re.IGNORECASE)
_RETRY_DELAY = re.compile(rf'"retryDelay"\s*:\s*"({_DURATION})"')
_PART = re.compile(r"(\d+(?:\.\d+)?)([dhms])")
# OpenRouter repeats its rate-limit headers inside the error body, and litellm
# does not always surface them as response headers.
_BODY_RESET = re.compile(r'"X-RateLimit-Reset"\s*:\s*"?(\d{10,13})"?', re.IGNORECASE)


def _seconds(duration: str) -> float:
    return sum(float(number) * _UNIT_SECONDS[unit] for number, unit in _PART.findall(duration))


def _header(headers: Mapping[str, Any] | None, name: str) -> str | None:
    for key, value in (headers or {}).items():
        lowered = str(key).lower()
        if lowered.startswith("llm_provider-"):
            lowered = lowered[len("llm_provider-"):]
        if lowered == name:
            return str(value)
    return None


def _parse_reset_header(headers: Mapping[str, Any] | None) -> float | None:
    """Parse OpenRouter's x-ratelimit-reset (epoch-ms) or x-ratelimit-reset-after (seconds)."""
    reset_ms = _header(headers, "x-ratelimit-reset")
    if reset_ms:
        try:
            reset_epoch = int(reset_ms) / 1000.0
            now = time.time()
            wait_seconds = reset_epoch - now
            if wait_seconds > 0:
                return wait_seconds
        except (ValueError, TypeError):
            pass
    # Also check for reset-after in seconds
    reset_after = _header(headers, "x-ratelimit-reset-after")
    if reset_after:
        try:
            return float(reset_after)
        except (ValueError, TypeError):
            pass
    return None


def _parse_body_reset(message: str | None) -> float | None:
    """Seconds until the ``X-RateLimit-Reset`` epoch quoted in an error body, if still ahead."""
    match = _BODY_RESET.search(message or "")
    if not match:
        return None
    reset = int(match.group(1))
    reset_epoch = reset / 1000.0 if reset > 10_000_000_000 else float(reset)
    wait_seconds = reset_epoch - time.time()
    return wait_seconds if wait_seconds > 0 else None


def retry_after_seconds(message: str | None, headers: Mapping[str, Any] | None = None) -> int | None:
    """Whole seconds the provider asked us to wait (1 to a day), or None if it did not say."""
    found: float | None = None

    # First, try OpenRouter's reset header (epoch-ms), then the same value in the body
    reset_wait = _parse_reset_header(headers)
    if reset_wait is None:
        reset_wait = _parse_body_reset(message)
    if reset_wait is not None:
        found = reset_wait
    else:
        # Fall back to Retry-After header
        header = _header(headers, "retry-after")
        if header is not None:
            try:
                found = float(header)
            except ValueError:
                found = None  # an HTTP date: not worth parsing for a pause

        # Then try text patterns
        if found is None and message:
            for pattern in (_RETRY_IN, _RETRY_DELAY):
                match = pattern.search(message)
                if match:
                    found = _seconds(match.group(1))
                    break

    if found is None or found <= 0:
        return None
    return int(min(MAX_HINT_SECONDS, max(1, math.ceil(found))))


def get_quota_reset_seconds(headers: Mapping[str, Any] | None) -> int | None:
    """Seconds until the quota resets, from OpenRouter's reset headers (1 to a day), or None."""
    reset_wait = _parse_reset_header(headers)
    if reset_wait is not None and reset_wait > 0:
        return int(min(MAX_HINT_SECONDS, max(1, math.ceil(reset_wait))))
    return None
