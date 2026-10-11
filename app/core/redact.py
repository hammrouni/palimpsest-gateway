"""Keep anything shaped like an API key out of text that is about to be logged."""
from __future__ import annotations

import re

_SECRET_LIKE = re.compile(
    r"(?:sk|pgw|gsk|nvapi|AIza)[-_A-Za-z0-9]{0,4}[-_][A-Za-z0-9_\-]{8,}|AIza[A-Za-z0-9_\-]{20,}"
)


def redact(text: str) -> str:
    """Replace key-shaped substrings with ``[REDACTED]``."""
    return _SECRET_LIKE.sub("[REDACTED]", text)
