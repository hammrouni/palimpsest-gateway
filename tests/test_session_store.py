"""Session store tests (owner-scoped access, TTL, history)."""

import pytest
from fakeredis.aioredis import FakeRedis

from app.session.store import SessionStore


@pytest.fixture
async def store():
    """Provide SessionStore with FakeRedis."""
    redis = FakeRedis()
    return SessionStore(redis, ttl_seconds=3600)


@pytest.mark.asyncio
async def test_create_session(store):
    """Create session returns record with id."""
    rec = await store.create_or_claim("account1")
    assert rec["id"]
    assert rec["created_at"]
    assert rec["turn_count"] == "0"
    assert rec["status"] == "active"


@pytest.mark.asyncio
async def test_claim_existing_session(store):
    """Re-claiming existing session is idempotent."""
    rec1 = await store.create_or_claim("account1", "my-session")
    rec2 = await store.create_or_claim("account1", "my-session")

    assert rec1["id"] == rec2["id"] == "my-session"


@pytest.mark.asyncio
async def test_tenant_scoped_sessions(store):
    """Account A's 'my-session' is different from account B's 'my-session'."""
    await store.create_or_claim("accountA", "my-session")
    await store.create_or_claim("accountB", "my-session")

    meta_a = await store.get_metadata("accountA", "my-session")
    meta_b = await store.get_metadata("accountB", "my-session")

    assert meta_a["id"] == "my-session"
    assert meta_b["id"] == "my-session"
    assert meta_a["created_at"] != meta_b["created_at"]


@pytest.mark.asyncio
async def test_foreign_session_not_found(store):
    """Get foreign session (different account_id) returns None."""
    await store.create_or_claim("accountA", "session1")

    meta = await store.get_metadata("accountB", "session1")
    assert meta is None


@pytest.mark.asyncio
async def test_append_and_read_history(store):
    """Append exchanges, read them back verbatim."""
    await store.create_or_claim("account1", "s1")

    messages1 = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi"},
    ]
    await store.append_exchange("account1", "s1", messages1)

    messages2 = [
        {"role": "user", "content": "how are you?"},
        {"role": "assistant", "content": "good"},
    ]
    await store.append_exchange("account1", "s1", messages2)

    recent = await store.read_recent("account1", "s1", 10)
    assert len(recent) == 2
    assert recent[0]["messages"] == messages1
    assert recent[1]["messages"] == messages2


@pytest.mark.asyncio
async def test_turn_count_increments(store):
    """turn_count increments on each append."""
    await store.create_or_claim("account1", "s1")

    for i in range(3):
        await store.append_exchange("account1", "s1", [{"role": "user", "content": f"msg{i}"}])

    rec = await store.get_metadata("account1", "s1")
    assert int(rec["turn_count"]) == 3


@pytest.mark.asyncio
async def test_ttl_set_on_create(store):
    """TTL is set on the record HASH and owner index SET after create.

    The history LIST key doesn't exist until the first exchange is appended
    (Redis LISTs are created on first RPUSH) — EXPIRE on a nonexistent key
    is a no-op, and that's correct: there's nothing to GC before it exists.
    """
    await store.create_or_claim("account1", "s1")

    record_key = store._session_key("account1", "s1")
    index_key = store._sessions_index_key("account1")

    assert await store.redis.ttl(record_key) > 0
    assert await store.redis.ttl(index_key) > 0


@pytest.mark.asyncio
async def test_ttl_refreshed_on_access(store):
    """TTL on the record, history, and owner index is refreshed on metadata read."""
    await store.create_or_claim("account1", "s1")
    await store.append_exchange("account1", "s1", [{"role": "user", "content": "hi"}])

    record_key = store._session_key("account1", "s1")
    history_key = store._history_key("account1", "s1")
    index_key = store._sessions_index_key("account1")

    # Force the TTLs down so a refresh is observable.
    await store.redis.expire(record_key, 10)
    await store.redis.expire(history_key, 10)
    await store.redis.expire(index_key, 10)

    await store.get_metadata("account1", "s1")

    assert await store.redis.ttl(record_key) > 10
    assert await store.redis.ttl(history_key) > 10
    assert await store.redis.ttl(index_key) > 10


@pytest.mark.asyncio
async def test_ttl_refreshed_on_append_and_read(store):
    """append_exchange and read_recent also refresh the owner index TTL."""
    await store.create_or_claim("account1", "s1")
    index_key = store._sessions_index_key("account1")

    await store.redis.expire(index_key, 10)
    await store.append_exchange("account1", "s1", [{"role": "user", "content": "hi"}])
    assert await store.redis.ttl(index_key) > 10

    await store.redis.expire(index_key, 10)
    await store.read_recent("account1", "s1", 10)
    assert await store.redis.ttl(index_key) > 10


@pytest.mark.asyncio
async def test_read_recent_returns_newest_n_in_order(store):
    """read_recent(count=N) returns the newest N exchanges, oldest-first within the window."""
    await store.create_or_claim("account1", "s1")

    for i in range(15):
        await store.append_exchange(
            "account1", "s1", [{"role": "user", "content": f"turn-{i}"}]
        )

    recent = await store.read_recent("account1", "s1", count=10)

    assert len(recent) == 10
    assert recent[0]["messages"][0]["content"] == "turn-5"
    assert recent[-1]["messages"][0]["content"] == "turn-14"


@pytest.mark.asyncio
async def test_read_recent_non_positive_count_returns_empty(store):
    """count<=0 returns no exchanges instead of the whole history."""
    await store.create_or_claim("account1", "s1")
    await store.append_exchange("account1", "s1", [{"role": "user", "content": "hi"}])

    assert await store.read_recent("account1", "s1", count=0) == []
    assert await store.read_recent("account1", "s1", count=-1) == []


@pytest.mark.asyncio
async def test_append_exchange_unclaimed_session_raises(store):
    """append_exchange on a session never create_or_claim'd fails clearly."""
    from app.session.store import SessionNotFoundError

    with pytest.raises(SessionNotFoundError):
        await store.append_exchange("account1", "never-claimed", [{"role": "user", "content": "hi"}])


# -- corrupted JSON in history is skipped, not fatal ----------------------


@pytest.mark.asyncio
async def test_read_recent_skips_corrupt_json_entry(store, caplog):
    """A malformed JSON entry in the history LIST is skipped with a
    structured warning; the remaining valid entries are returned. The
    gateway never 500s on one corrupt entry."""
    import logging as _logging

    await store.create_or_claim("account1", "s1")
    # Append one valid entry.
    await store.append_exchange("account1", "s1", [{"role": "user", "content": "valid-1"}])
    # Inject a corrupt entry directly into the history LIST.
    hist_key = store._history_key("account1", "s1")
    await store.redis.rpush(hist_key, "not-valid-json-{{{")
    # Append another valid entry.
    await store.append_exchange("account1", "s1", [{"role": "user", "content": "valid-2"}])

    with caplog.at_level(_logging.WARNING, logger="palimpsest.session"):
        recent = await store.read_recent("account1", "s1", count=10)

    # Two valid entries returned, corrupt one skipped.
    assert len(recent) == 2
    assert recent[0]["messages"][0]["content"] == "valid-1"
    assert recent[1]["messages"][0]["content"] == "valid-2"
    assert any("history_entry_corrupt" in m for m in caplog.messages)


# -- history LIST is bounded -----------------------------------------------


@pytest.mark.asyncio
async def test_append_exchange_trims_history_to_max_exchanges():
    """Appending beyond max_exchanges trims oldest entries; newest
    retained; the cap is enforced at the writer (append_exchange), not the
    reader (read_recent)."""
    redis = FakeRedis()
    small_store = SessionStore(redis, ttl_seconds=3600, max_exchanges=3)
    await small_store.create_or_claim("account1", "s1")

    for i in range(5):
        await small_store.append_exchange(
            "account1", "s1", [{"role": "user", "content": f"turn-{i}"}]
        )

    # History LIST length is capped at max_exchanges (3).
    hist_key = small_store._history_key("account1", "s1")
    assert await small_store.redis.llen(hist_key) == 3

    # The 3 most recent entries are retained (turn-2, turn-3, turn-4).
    recent = await small_store.read_recent("account1", "s1", count=10)
    assert len(recent) == 3
    assert recent[0]["messages"][0]["content"] == "turn-2"
    assert recent[1]["messages"][0]["content"] == "turn-3"
    assert recent[2]["messages"][0]["content"] == "turn-4"


@pytest.mark.asyncio
async def test_default_max_exchanges_matches_shared_constant(store):
    """The default max_exchanges is MAX_HISTORY_EXCHANGES."""
    from app.session.store import MAX_HISTORY_EXCHANGES

    assert store.max_exchanges == MAX_HISTORY_EXCHANGES == 1000


# -- stale session index entries are cleaned up -----------------------------


@pytest.mark.asyncio
async def test_get_metadata_cleans_stale_index_entries(store):
    """A stale ID in the owner's session index SET (whose session
    record has expired) is removed when get_metadata is called on a live
    session. Best-effort cleanup, non-blocking."""
    await store.create_or_claim("account1", "live-session")
    await store.create_or_claim("account1", "stale-session")

    index_key = store._sessions_index_key("account1")
    # Sanity: both sessions are in the index.
    members = await store.redis.smembers(index_key)
    assert {m.decode() if isinstance(m, bytes) else m for m in members} == {
        "live-session",
        "stale-session",
    }

    # Expire the record for "stale-session" (simulating TTL elapsing).
    stale_record_key = store._session_key("account1", "stale-session")
    await store.redis.delete(stale_record_key)

    # get_metadata on a live session triggers best-effort cleanup.
    await store.get_metadata("account1", "live-session")

    members = await store.redis.smembers(index_key)
    assert {m.decode() if isinstance(m, bytes) else m for m in members} == {"live-session"}


@pytest.mark.asyncio
async def test_stale_cleanup_failure_does_not_block_read(store, monkeypatch):
    """A Redis hiccup in the cleanup path never blocks the read."""
    await store.create_or_claim("account1", "s1")

    async def _raise(*args, **kwargs):
        raise RuntimeError("simulated Redis outage")

    monkeypatch.setattr(store, "_cleanup_stale_sessions", _raise)

    # The read still returns the record despite the cleanup failure.
    meta = await store.get_metadata("account1", "s1")
    assert meta is not None
    assert meta["id"] == "s1"


# -- read_recent count is capped --------------------------------------------


@pytest.mark.asyncio
async def test_read_recent_clamps_count_to_max(store):
    """read_recent(count=N) with N > MAX_HISTORY_EXCHANGES is clamped
    to the shared ceiling — never an arbitrarily large LRANGE."""
    from app.session.store import MAX_HISTORY_EXCHANGES

    await store.create_or_claim("account1", "s1")
    # Append some exchanges.
    for i in range(3):
        await store.append_exchange(
            "account1", "s1", [{"role": "user", "content": f"t-{i}"}]
        )

    # An absurdly large count is clamped to MAX_HISTORY_EXCHANGES — the
    # LRANGE never escapes the cap (FakeRedis LRANGE is a no-op on a
    # short list, so the result is the same as the real list).
    recent = await store.read_recent("account1", "s1", count=MAX_HISTORY_EXCHANGES * 100)
    assert len(recent) == 3


# -- get_metadata returns turn_count as int --------------------------------


@pytest.mark.asyncio
async def test_get_metadata_turn_count_is_int(store):
    """turn_count is returned as int, not the Redis HSET string —
    downstream consumers (e.g. rebuild()'s SessionRef) get a typed value."""
    await store.create_or_claim("account1", "s1")
    for _ in range(3):
        await store.append_exchange("account1", "s1", [{"role": "user", "content": "x"}])
    meta = await store.get_metadata("account1", "s1")
    assert isinstance(meta["turn_count"], int)
    assert meta["turn_count"] == 3
    # No string-to-int conversion needed at the call site.
    assert meta["turn_count"] + 1 == 4


@pytest.mark.asyncio
async def test_get_metadata_handles_corrupt_turn_count(store):
    """A corrupt (non-numeric) turn_count degrades to 0 with a
    structured warning, never a 500."""
    record_key = store._session_key("account1", "s1")
    await store.create_or_claim("account1", "s1")
    # Manually inject a corrupt turn_count.
    await store.redis.hset(record_key, "turn_count", "not-a-number")
    meta = await store.get_metadata("account1", "s1")
    assert meta["turn_count"] == 0

