"""How long a provider says to wait before trying again.

A 429 usually carries the answer: a ``Retry-After`` header, or text such as Google's
"Please retry in 7h14m58.6s" and its ``"retryDelay": "37s"`` field. Pausing a key for
exactly that long beats a fixed guess: a per-minute limit comes back in seconds, a
daily quota in hours, and hammering the second kind every minute only wastes calls.
"""
from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

MAX_HINT_SECONDS = 86400  # a provider asking for more than a day is not trusted
_UNIT_SECONDS = {"d": 86400, "h": 3600, "m": 60, "s": 1}

_DURATION = r"(?:\d+(?:\.\d+)?[dhms])+"
_RETRY_IN = re.compile(rf"retry in\s+({_DURATION})", re.IGNORECASE)
_RETRY_DELAY = re.compile(rf'"retryDelay"\s*:\s*"({_DURATION})"')
_PART = re.compile(r"(\d+(?:\.\d+)?)([dhms])")


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


def retry_after_seconds(message: str | None, headers: Mapping[str, Any] | None = None) -> int | None:
    """Whole seconds the provider asked us to wait (1 to a day), or None if it did not say."""
    found: float | None = None

    header = _header(headers, "retry-after")
    if header is not None:
        try:
            found = float(header)
        except ValueError:
            found = None  # an HTTP date: not worth parsing for a pause

    if found is None and message:
        for pattern in (_RETRY_IN, _RETRY_DELAY):
            match = pattern.search(message)
            if match:
                found = _seconds(match.group(1))
                break

    if found is None or found <= 0:
        return None
    return int(min(MAX_HINT_SECONDS, max(1, math.ceil(found))))
