"""Live request feed: what each request is doing, as it happens.

The dashboard's Live Flow page draws a request leaving a client, entering the
gateway, going to a provider and model, and the answer coming back. This module
carries those events from the request path to that page.

Operator-only metadata: account, key, provider and model ids, status codes,
token counts and timings. Never message content or key material.

* Events go into one capped Redis stream, so a reader can ask for "everything
  after this id" and never miss or repeat one, whatever order writers finish in.
* Nothing is written unless somebody is watching. Each poll from the page
  refreshes a short-lived marker; a request checks it once when it starts and
  only then emits. With the page closed the cost is one ``EXISTS`` per request.

Events are ``{"rid", "kind", ...}`` with ``kind`` one of:

* ``request``: a request arrived (``account_id``, ``requested``, ``model``,
  ``routed_by``, ``stream``)
* ``attempt``: the gateway sent it to a key (``n``, ``key_id``, ``provider``,
  ``model``)
* ``result``: that attempt came back (``ok``, ``status_code``,
  ``classification``, ``ms``, ...)
* ``hop``: supermode moved to another model (``from``, ``to``, ``cause``)
* ``done``: the answer went back to the client (``status_code``, ``outcome``,
  ``latency_ms``, token counts, ...)
"""
from __future__ import annotations

import json
import logging
import time
from typing import Any

logger = logging.getLogger("palimpsest.live")

_STREAM = "pgw:live:events"
_WATCH = "pgw:live:watch"
# Events kept, and how long a poll keeps the feed switched on.
MAX_EVENTS = 600
WATCH_SECONDS = 20


async def watching(redis: Any) -> bool:
    """Whether the Live Flow page has polled lately."""
    try:
        return bool(await redis.exists(_WATCH))
    except Exception:
        logger.warning("could not read the live marker", exc_info=True)
        return False


async def emit(redis: Any, event: dict[str, Any]) -> None:
    """Add one event to the feed. Best effort: a fault here never fails a request."""
    try:
        payload = json.dumps({**event, "t": int(time.time() * 1000)}, default=str)
        await redis.xadd(_STREAM, {"e": payload}, maxlen=MAX_EVENTS, approximate=False)
    except Exception:
        logger.warning("could not record a live event", exc_info=True)


def _text(value: Any) -> str:
    return value.decode() if isinstance(value, bytes) else str(value)


async def read(redis: Any, after: str | None, limit: int = 300) -> tuple[list[dict[str, Any]], str | None]:
    """Events after the id ``after`` (the newest ones when ``None``), and the new cursor.

    Also keeps the feed switched on for ``WATCH_SECONDS``.
    """
    await redis.set(_WATCH, "1", ex=WATCH_SECONDS)
    if after:
        rows = await redis.xrange(_STREAM, min=after, max="+", count=limit + 1)
        rows = [row for row in rows if _text(row[0]) != after][:limit]
    else:
        rows = list(reversed(await redis.xrevrange(_STREAM, max="+", min="-", count=limit)))
    events: list[dict[str, Any]] = []
    cursor = after
    for entry_id, fields in rows:
        cursor = _text(entry_id)
        raw = fields.get(b"e") if b"e" in fields else fields.get("e")
        try:
            events.append({**json.loads(_text(raw)), "id": cursor})
        except (TypeError, ValueError):
            continue
    return events, cursor
