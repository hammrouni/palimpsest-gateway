"""The rebuild hook the failover loop calls between attempts.

``app.context.rebuild.rebuild`` is the real implementation.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable

RebuildHook = Callable[[list[dict], object, int], Awaitable[list[dict]]]
"""(messages, session, attempt_index) -> messages for the next attempt."""


async def identity_rebuild(
    messages: list[dict], session: object, attempt_index: int
) -> list[dict]:
    """Resend the messages unchanged."""
    return messages
