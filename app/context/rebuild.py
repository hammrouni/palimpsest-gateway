"""Failover context: what a replacement provider receives.

``rebuild()`` is the failover loop's rebuild hook. It hands the next provider
the client's instructions, the last N stored turns and the current turn. It
reads only session history, through ``SessionStore``.
"""

from __future__ import annotations

from dataclasses import dataclass

from redis.asyncio import Redis

from app.config import get_settings
from app.session.store import MAX_HISTORY_EXCHANGES, SessionStore


@dataclass(frozen=True)
class SessionRef:
    """The session a failover rebuild reads from, plus the shared Redis client."""

    account_id: str
    session_id: str
    turn_count: int
    redis: Redis


def _flatten(exchanges: list[dict]) -> list[dict]:
    """Flatten stored ``{"ts": ..., "messages": [...]}`` exchanges into one list."""
    flat: list[dict] = []
    for exchange in exchanges:
        if not isinstance(exchange, dict):
            continue
        messages = exchange.get("messages")
        if not isinstance(messages, list):
            continue
        flat.extend(messages)
    return flat


def _last_n(exchanges: list[dict], n: int) -> list[dict]:
    """The last ``n`` exchanges in append order."""
    if n <= 0:
        return []
    return exchanges[-n:]


_INSTRUCTION_ROLES = frozenset({"system", "developer"})


def _instructions(messages: list[dict]) -> list[dict]:
    """The leading system/developer messages of the inbound array."""
    out: list[dict] = []
    for message in messages:
        if message.get("role") not in _INSTRUCTION_ROLES:
            break
        out.append(message)
    return out


def _current_turn(messages: list[dict]) -> list[dict]:
    """The current turn: the last user message and everything after it.

    What follows it (assistant ``tool_calls`` and their ``tool`` results)
    belongs to the same turn. Never raises.
    """
    for index in range(len(messages) - 1, -1, -1):
        if messages[index].get("role") == "user":
            return messages[index:]
    if messages:
        return messages[-1:]
    return [{"role": "user", "content": ""}]


def client_sends_history(messages: list[dict]) -> bool:
    """Whether the client resends earlier turns itself.

    An assistant or tool message before the last user message can only be an
    earlier turn; after it, it is the current turn's own tool use.
    """
    current = _current_turn(messages)
    earlier = messages[: len(messages) - len(current)]
    return any(m.get("role") in ("assistant", "tool") for m in earlier)


def _strip_tool_messages(messages: list[dict]) -> list[dict]:
    """Remove ``tool_calls`` and tool-role messages when no ``tools`` are defined.

    A prior turn that used tools leaves assistant messages with ``tool_calls``
    and ``tool``-role result messages in the persisted exchange. A later turn
    that arrives without a ``tools`` definition leaves those calls dangling;
    every provider rejects a conversation that carries ``tool_calls`` without a
    matching ``tools`` parameter with HTTP 400. Stripping the tool scaffolding
    from the history half yields a clean, provider-valid conversation for the
    new model.

    An assistant message that *only* carries ``tool_calls`` (content is None or
    empty) is dropped entirely. One that also has real text content is kept with
    the ``tool_calls`` field removed.
    """
    result: list[dict] = []
    for msg in messages:
        role = msg.get("role")
        if role == "tool":
            continue
        if role == "assistant" and msg.get("tool_calls"):
            msg = dict(msg)
            del msg["tool_calls"]
            content = msg.get("content")
            if content is None or content == "":
                continue
        result.append(msg)
    return result


def first_attempt_messages(
    messages: list[dict], exchanges: list[dict], *, has_tools: bool = False
) -> list[dict]:
    """What the first provider attempt receives.

    A client that resends history already sent everything. Otherwise stored
    exchanges go after the client's instructions, with stored copies of those
    instructions dropped so they appear once.

    When ``has_tools`` is false (the request supplies no non-empty ``tools``
    definition), tool calls and tool-role messages are removed from the history
    so a prior tool-using turn cannot make the new model's provider reject the
    request with HTTP 400.
    """
    if client_sends_history(messages):
        result = list(messages)
        return _strip_tool_messages(result) if not has_tools else result
    instructions = _instructions(messages)
    history = [m for m in _flatten(exchanges) if m not in instructions]
    if not has_tools:
        history = _strip_tool_messages(history)
    return [*instructions, *history, *messages[len(instructions) :]]


def turn_to_persist(messages: list[dict]) -> list[dict]:
    """The new part of the inbound messages, for session history.

    Storing a history-resending client's whole array would make every stored
    exchange contain all previous ones.
    """
    if client_sends_history(messages):
        return [*_instructions(messages), *_current_turn(messages)]
    return list(messages)


async def rebuild(
    messages: list[dict], session: SessionRef, attempt_index: int
) -> list[dict]:
    """Instructions + last-N stored turns + the current turn.

    Deterministic: every failover of a turn gets the same context. With no
    readable stored history the messages are resent unchanged, so a client
    that manages its own history keeps it.
    """
    n = get_settings().verbatim_turns_n
    store = SessionStore(session.redis)
    try:
        stored = await store.read_recent(
            session.account_id, session.session_id, count=MAX_HISTORY_EXCHANGES
        )
    except Exception:
        stored = []  # never raise into the failover loop

    if not stored:
        return list(messages)

    instructions = _instructions(messages)
    window = [m for m in _flatten(_last_n(stored, n)) if m not in instructions]
    return [*instructions, *window, *_current_turn(messages)]
