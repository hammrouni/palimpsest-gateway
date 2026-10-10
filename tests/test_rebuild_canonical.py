"""Failover context reconstruction: instructions + last-N stored turns + current turn."""

from __future__ import annotations

import ast
import pathlib
from unittest.mock import AsyncMock

import pytest
from fakeredis.aioredis import FakeRedis

from app.config import get_settings
from app.context.rebuild import (
    SessionRef,
    _strip_tool_messages,
    first_attempt_messages,
    rebuild,
)
from app.session.store import MAX_HISTORY_EXCHANGES, SessionStore

ACCOUNT_A = "acct-a"
ACCOUNT_B = "acct-b"
SESSION_ID = "sess-1"


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def store(redis):
    return SessionStore(redis)


async def _seed_exchanges(
    store: SessionStore, account_id: str, session_id: str, count: int
) -> None:
    for i in range(count):
        await store.append_exchange(
            account_id,
            session_id,
            [
                {"role": "user", "content": f"user turn {i}"},
                {"role": "assistant", "content": f"assistant turn {i}"},
            ],
        )


def _current_prompt(text: str = "current prompt") -> list[dict]:
    return [{"role": "user", "content": text}]


async def _session_ref(
    store: SessionStore, account_id: str, session_id: str
) -> SessionRef:
    metadata = await store.get_metadata(account_id, session_id)
    turn_count = int(metadata["turn_count"]) if metadata else 0
    return SessionRef(
        account_id=account_id,
        session_id=session_id,
        turn_count=turn_count,
        redis=store.redis,
    )


@pytest.mark.asyncio
async def test_exact_shape(store):
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    await _seed_exchanges(store, ACCOUNT_A, SESSION_ID, count=6)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    out = await rebuild(_current_prompt(), session, attempt_index=1)

    verbatim = out[:-1]
    assert len(verbatim) == 8  # last 4 exchanges x 2 messages
    assert verbatim[0] == {"role": "user", "content": "user turn 2"}
    assert verbatim[-1] == {"role": "assistant", "content": "assistant turn 5"}
    assert out[-1] == {"role": "user", "content": "current prompt"}


@pytest.mark.asyncio
async def test_output_never_injects_a_summary_role(store):
    """No synthesized context: only stored turns, verbatim."""
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    await _seed_exchanges(store, ACCOUNT_A, SESSION_ID, count=9)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    out = await rebuild(_current_prompt(), session, attempt_index=1)

    assert all(m.get("role") != "system" for m in out)


@pytest.mark.asyncio
async def test_verbatim_turns_n_setting(store, monkeypatch):
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    await _seed_exchanges(store, ACCOUNT_A, SESSION_ID, count=5)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    monkeypatch.setenv("PALIMPSEST_VERBATIM_TURNS_N", "2")
    get_settings.cache_clear()
    try:
        out = await rebuild(_current_prompt(), session, attempt_index=1)
    finally:
        get_settings.cache_clear()

    verbatim = out[:-1]
    assert len(verbatim) == 4  # last 2 exchanges x 2 messages
    assert verbatim[0] == {"role": "user", "content": "user turn 3"}


@pytest.mark.asyncio
async def test_default_n_is_four(store):
    get_settings.cache_clear()
    assert get_settings().verbatim_turns_n == 4
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    await _seed_exchanges(store, ACCOUNT_A, SESSION_ID, count=6)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    out = await rebuild(_current_prompt(), session, attempt_index=1)

    assert len(out[:-1]) == 8


@pytest.mark.asyncio
async def test_fewer_stored_turns_than_n_uses_all_without_padding(store):
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    await _seed_exchanges(store, ACCOUNT_A, SESSION_ID, count=2)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    out = await rebuild(_current_prompt(), session, attempt_index=1)

    verbatim = out[:-1]
    assert len(verbatim) == 4
    assert verbatim[0] == {"role": "user", "content": "user turn 0"}
    assert out[-1]["content"] == "current prompt"


@pytest.mark.asyncio
async def test_no_history_resends_the_messages_unchanged(store):
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    out = await rebuild(_current_prompt(), session, attempt_index=1)

    assert out == [{"role": "user", "content": "current prompt"}]


@pytest.mark.asyncio
async def test_client_managed_history_survives_failover_without_stored_turns(store):
    """A client that resends its own history keeps it when nothing is stored."""
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)
    inbound = [
        {"role": "system", "content": "be brief"},
        {"role": "user", "content": "my name is Ada"},
        {"role": "assistant", "content": "hello Ada"},
        {"role": "user", "content": "what is my name?"},
    ]

    out = await rebuild(inbound, session, attempt_index=1)

    assert out == inbound


def test_context_package_has_no_forbidden_imports():
    forbidden_modules = {"app.provider", "app.router"}
    context_dir = pathlib.Path("app/context")
    for path in context_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module] if node.module else []
            else:
                continue
            for name in names:
                for forbidden in forbidden_modules:
                    assert not (
                        name == forbidden or name.startswith(forbidden + ".")
                    ), f"{path} imports forbidden module {name!r}"


def test_no_pool_key_id_reference_in_context_package():
    context_dir = pathlib.Path("app/context")
    for path in context_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "key_id" not in text


def test_context_package_makes_no_outbound_service_calls():
    """The rebuild is a pure read of session history: no HTTP."""
    forbidden = ("httpx", "aiohttp", "requests", "urllib.request")
    context_dir = pathlib.Path("app/context")
    for path in context_dir.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for token in forbidden:
            assert token not in text, f"{path} references {token!r}"


@pytest.mark.asyncio
async def test_no_provider_identity_in_output(store):
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    await _seed_exchanges(store, ACCOUNT_A, SESSION_ID, count=6)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    out = await rebuild(_current_prompt(), session, attempt_index=1)

    serialized = str(out)
    for leaked in ("openrouter", "anthropic", "key_id", get_settings().provider_model):
        assert leaked not in serialized


@pytest.mark.asyncio
async def test_foreign_account_never_reads_another_accounts_history(store):
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    await _seed_exchanges(store, ACCOUNT_A, SESSION_ID, count=6)
    session_a = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    out_a = await rebuild(_current_prompt(), session_a, attempt_index=1)
    assert "user turn 2" in str(out_a)

    # Same session_id, another account: a separate key.
    await store.create_or_claim(ACCOUNT_B, SESSION_ID)
    session_b = await _session_ref(store, ACCOUNT_B, SESSION_ID)
    out_b = await rebuild(_current_prompt(), session_b, attempt_index=1)

    assert "user turn 2" not in str(out_b)
    assert out_b == [{"role": "user", "content": "current prompt"}]


@pytest.mark.asyncio
async def test_current_prompt_comes_once_with_stored_turns_not_inbound_history(store):
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    await _seed_exchanges(store, ACCOUNT_A, SESSION_ID, count=6)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    inbound = [
        {"role": "user", "content": "client-resent old turn 1"},
        {"role": "assistant", "content": "client-resent old reply 1"},
        {"role": "user", "content": "THE CURRENT PROMPT"},
    ]

    out = await rebuild(inbound, session, attempt_index=1)

    current_occurrences = [m for m in out if m.get("content") == "THE CURRENT PROMPT"]
    assert len(current_occurrences) == 1
    assert out[-1]["content"] == "THE CURRENT PROMPT"
    assert not any(m["content"] == "client-resent old turn 1" for m in out)


@pytest.mark.asyncio
async def test_rebuild_performs_no_provider_or_registry_calls(store, monkeypatch):
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    await _seed_exchanges(store, ACCOUNT_A, SESSION_ID, count=6)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    attempt_spy = AsyncMock()
    mark_exhausted_spy = AsyncMock()
    mark_cooldown_spy = AsyncMock()
    monkeypatch.setattr("app.provider.adapter.attempt", attempt_spy)
    monkeypatch.setattr(
        "app.pool.registry.PoolRegistry.mark_exhausted", mark_exhausted_spy
    )
    monkeypatch.setattr(
        "app.pool.registry.PoolRegistry.mark_cooldown", mark_cooldown_spy
    )

    await rebuild(_current_prompt(), session, attempt_index=1)
    await rebuild(_current_prompt(), session, attempt_index=2)

    attempt_spy.assert_not_called()
    mark_exhausted_spy.assert_not_called()
    mark_cooldown_spy.assert_not_called()


@pytest.mark.asyncio
async def test_read_error_resends_the_messages_unchanged(store, monkeypatch):
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    await _seed_exchanges(store, ACCOUNT_A, SESSION_ID, count=6)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    async def raise_redis_error(self, *args, **kwargs):
        raise RuntimeError("redis down")

    monkeypatch.setattr(SessionStore, "read_recent", raise_redis_error)

    out = await rebuild(_current_prompt(), session, attempt_index=1)

    assert out == [{"role": "user", "content": "current prompt"}]


@pytest.mark.asyncio
async def test_empty_messages_does_not_raise(store):
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    await _seed_exchanges(store, ACCOUNT_A, SESSION_ID, count=6)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    out = await rebuild([], session, attempt_index=1)

    assert out[-1] == {"role": "user", "content": ""}


@pytest.mark.asyncio
async def test_read_recent_uses_max_history_exchanges(store, monkeypatch):
    """rebuild() reads with count=MAX_HISTORY_EXCHANGES, not the default 10."""
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    await _seed_exchanges(store, ACCOUNT_A, SESSION_ID, count=6)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    call_args = []
    original_read_recent = SessionStore.read_recent

    # Class-level patch: rebuild() builds its own SessionStore.
    async def spy(self, account_id, session_id, count=10):
        call_args.append(count)
        return await original_read_recent(self, account_id, session_id, count=count)

    monkeypatch.setattr(SessionStore, "read_recent", spy)

    await rebuild(_current_prompt(), session, attempt_index=1)

    assert call_args == [MAX_HISTORY_EXCHANGES]


@pytest.mark.asyncio
async def test_repeated_failovers_rebuild_identical_context(store):
    await store.create_or_claim(ACCOUNT_A, SESSION_ID)
    await _seed_exchanges(store, ACCOUNT_A, SESSION_ID, count=7)
    session = await _session_ref(store, ACCOUNT_A, SESSION_ID)

    attempts = [
        await rebuild(_current_prompt(), session, attempt_index=i) for i in range(4)
    ]

    assert all(a == attempts[0] for a in attempts)


# -- orphaned tool-call stripping -------------------------------------------------


def test_strip_tool_messages_removes_tool_role_and_tool_calls():
    msgs = [
        {"role": "assistant", "content": None, "tool_calls": [{"id": "t1"}]},
        {"role": "tool", "tool_call_id": "t1", "content": "sunny"},
        {"role": "assistant", "content": "ok"},
    ]
    out = _strip_tool_messages(msgs)
    assert out == [
        {"role": "assistant", "content": "ok"},
    ]


def test_strip_tool_messages_preserves_regular_history():
    msgs = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    assert _strip_tool_messages(msgs) == msgs


def test_first_attempt_messages_strips_history_when_no_tools():
    stored = [
        {
            "messages": [
                {"role": "user", "content": "weather?"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "t"}]},
                {"role": "tool", "tool_call_id": "t", "content": "sunny"},
                {"role": "assistant", "content": "it is sunny"},
            ]
        }
    ]
    out = first_attempt_messages(
        [{"role": "user", "content": "next turn"}], stored, has_tools=False
    )
    assert not any(m.get("role") == "tool" for m in out)
    assert not any(m.get("tool_calls") for m in out)
    assert {"role": "user", "content": "weather?"} in out
    assert {"role": "assistant", "content": "it is sunny"} in out


def test_first_attempt_messages_keeps_history_when_has_tools():
    stored = [
        {
            "messages": [
                {"role": "user", "content": "weather?"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "t"}]},
                {"role": "tool", "tool_call_id": "t", "content": "sunny"},
            ]
        }
    ]
    out = first_attempt_messages(
        [{"role": "user", "content": "next turn"}], stored, has_tools=True
    )
    assert any(m.get("tool_calls") for m in out)
    assert any(m.get("role") == "tool" for m in out)
