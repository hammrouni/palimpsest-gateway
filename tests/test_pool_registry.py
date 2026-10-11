"""Provider Key Pool registry."""
from datetime import UTC, datetime, time, timedelta

import pytest
from fakeredis.aioredis import FakeRedis
from pydantic import ValidationError

from app.pool.models import KeyCreate, KeyState, Provider
from app.pool.registry import (
    DuplicateKeyError,
    KeyNotFoundError,
    PoolRegistry,
    _key_hint,
)

# PALIMPSEST_GATE_MASTER_KEY is provided session-wide by tests/conftest.py's
# autouse ``_pool_master_key`` fixture (the process-wide Fernet manager needs it).


@pytest.fixture
async def registry():
    # decode_responses=True matches the production client (app/main.init_store).
    return PoolRegistry(FakeRedis(decode_responses=True))


def _state_key(key_id: str) -> str:
    return f"pgw:pool:state:{key_id}"


def _record_key(key_id: str) -> str:
    return f"pgw:pool:keys:{key_id}"


def _seconds_to_next_midnight(now: datetime) -> float:
    midnight = datetime.combine(now.date() + timedelta(days=1), time.min, tzinfo=UTC)
    return (midnight - now).total_seconds()


# -- KeyCreate validation ------------------------------------------------


def test_keycreate_rejects_past_reset_at():
    with pytest.raises(ValidationError):
        KeyCreate(
            provider=Provider.OPENAI,
            models=["m"],
            key="k",
            reset_at=datetime.now(UTC) - timedelta(hours=1),
        )


def test_keycreate_rejects_naive_reset_at():
    with pytest.raises(ValidationError):
        KeyCreate(
            provider=Provider.OPENAI,
            models=["m"],
            key="k",
            reset_at=datetime(2030, 1, 1),  # zone-less → would silently mean UTC
        )


def test_keycreate_rejects_empty_models():
    with pytest.raises(ValidationError):
        KeyCreate(provider=Provider.OPENAI, models=[], key="k")


def test_keycreate_rejects_unknown_provider():
    with pytest.raises(ValidationError):
        KeyCreate(provider="not-a-provider", models=["m"], key="k")


def test_keycreate_rejects_out_of_range_reset_period_hours():
    with pytest.raises(ValidationError):
        KeyCreate(provider=Provider.OPENAI, models=["m"], key="k", reset_period_hours=0)
    with pytest.raises(ValidationError):
        KeyCreate(provider=Provider.OPENAI, models=["m"], key="k", reset_period_hours=721)


def test_keycreate_rejects_invalid_daily_cap():
    with pytest.raises(ValidationError):
        KeyCreate(provider=Provider.OPENAI, models=["m"], key="k", daily_cap=-1)
    with pytest.raises(ValidationError):
        KeyCreate(provider=Provider.OPENAI, models=["m"], key="k", daily_cap=float("nan"))
    with pytest.raises(ValidationError):
        KeyCreate(provider=Provider.OPENAI, models=["m"], key="k", daily_cap=float("inf"))


def test_keycreate_strips_and_rejects_blank_key():
    with pytest.raises(ValidationError):
        KeyCreate(provider=Provider.OPENAI, models=["m"], key="   ")
    data = KeyCreate(provider=Provider.OPENAI, models=["m"], key="  padded  ")
    assert data.key == "padded"


# -- add + routable + non-exposure ------------------------------------------


@pytest.mark.asyncio
async def test_create_key_active_and_routable(registry: PoolRegistry):
    canary = "canary-raw-material-abc123"
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["gpt-4o-mini"], key=canary)
    )

    assert record.state == KeyState.ACTIVE
    assert canary not in record.key_hint
    assert canary not in record.model_dump_json()

    routable_ids = [r.id for r in await registry.routable_keys()]
    assert record.id in routable_ids

    routable_by_model = [r.id for r in await registry.routable_keys(model="gpt-4o-mini")]
    assert record.id in routable_by_model

    routable_other_model = [r.id for r in await registry.routable_keys(model="other-model")]
    assert record.id not in routable_other_model


def test_key_hint_masks_short_and_long_material():
    # <= 7 chars: first-3 + last-4 would disclose nearly the whole value.
    short = "sk-7chr"
    hint = _key_hint(short)
    assert short not in hint
    assert len(hint) <= 2
    long_key = "canary-material-0123456789"
    assert _key_hint(long_key) == "can…6789"
    assert long_key not in _key_hint(long_key)


# -- duplicate digest -------------------------------------------------------


@pytest.mark.asyncio
async def test_duplicate_key_material_rejected(registry: PoolRegistry):
    await registry.create(KeyCreate(provider=Provider.OPENAI, models=["m"], key="dup-material"))

    with pytest.raises(DuplicateKeyError):
        await registry.create(
            KeyCreate(provider=Provider.ANTHROPIC, models=["claude"], key="dup-material")
        )

    assert len(await registry.list()) == 1


@pytest.mark.asyncio
async def test_padded_key_material_deduped_through_strip(registry: PoolRegistry):
    await registry.create(KeyCreate(provider=Provider.OPENAI, models=["m"], key="pad-me"))

    with pytest.raises(DuplicateKeyError):
        await registry.create(
            KeyCreate(provider=Provider.ANTHROPIC, models=["m"], key="  pad-me  ")
        )


# -- suspend/resume, orthogonal to exhaustion --------------------------


@pytest.mark.asyncio
async def test_suspend_resume_immediate_and_idempotent(registry: PoolRegistry):
    record = await registry.create(KeyCreate(provider=Provider.OPENAI, models=["m"], key="k1"))

    suspended = await registry.suspend(record.id)
    assert suspended.state == KeyState.SUSPENDED
    assert record.id not in [r.id for r in await registry.routable_keys()]

    suspended_again = await registry.suspend(record.id)
    assert suspended_again.state == KeyState.SUSPENDED

    resumed = await registry.resume(record.id)
    assert resumed.state == KeyState.ACTIVE
    assert record.id in [r.id for r in await registry.routable_keys()]

    resumed_again = await registry.resume(record.id)
    assert resumed_again.state == KeyState.ACTIVE


@pytest.mark.asyncio
async def test_mutation_attribution_recorded(registry: PoolRegistry):
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["m"], key="k-op"), operator_id="op-1"
    )
    assert record.operator_id == "op-1"
    assert record.last_modified_by == "op-1"

    suspended = await registry.suspend(record.id, operator_id="op-2")
    assert suspended.operator_id == "op-1"
    assert suspended.last_modified_by == "op-2"

    resumed = await registry.resume(record.id, operator_id="op-1")
    assert resumed.last_modified_by == "op-1"


@pytest.mark.asyncio
async def test_exhausted_survives_resume_but_not_suspend_priority(registry: PoolRegistry):
    record = await registry.create(KeyCreate(provider=Provider.OPENAI, models=["m"], key="k2"))

    await registry.mark_exhausted(record.id)
    assert await registry.effective_state(record.id) == KeyState.EXHAUSTED

    # Resume (no prior suspend) does not clear a live exhaustion marker.
    await registry.resume(record.id)
    assert await registry.effective_state(record.id) == KeyState.EXHAUSTED

    # Suspend on top of exhaustion → suspended takes priority (highest priority axis).
    await registry.suspend(record.id)
    assert await registry.effective_state(record.id) == KeyState.SUSPENDED


@pytest.mark.asyncio
async def test_suspended_takes_priority_over_cooldown_marker(registry: PoolRegistry):
    record = await registry.create(KeyCreate(provider=Provider.OPENAI, models=["m"], key="k2b"))

    await registry.mark_cooldown(record.id)
    assert await registry.effective_state(record.id) == KeyState.COOLDOWN

    await registry.suspend(record.id)
    assert await registry.effective_state(record.id) == KeyState.SUSPENDED

    await registry.resume(record.id)
    # Resume clears only the suspension flag — the marker is still live.
    assert await registry.effective_state(record.id) == KeyState.COOLDOWN


# -- TTL math for exhausted (default) and cooldown ---------------------


@pytest.mark.asyncio
async def test_mark_exhausted_default_ttl_hits_next_midnight(registry: PoolRegistry):
    record = await registry.create(KeyCreate(provider=Provider.OPENAI, models=["m"], key="k3"))

    before = datetime.now(UTC)
    await registry.mark_exhausted(record.id)
    after = datetime.now(UTC)

    ttl = await registry.redis.ttl(_state_key(record.id))
    lo = _seconds_to_next_midnight(after) - 5
    hi = _seconds_to_next_midnight(before) + 5
    assert lo <= ttl <= hi
    assert 0 < ttl <= 24 * 3600
    assert record.id not in [r.id for r in await registry.routable_keys()]


@pytest.mark.asyncio
async def test_mark_cooldown_ttl_tracks_settings_override(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    from app.config import get_settings

    monkeypatch.setenv("PALIMPSEST_COOLDOWN_SECONDS", "2")
    get_settings.cache_clear()
    try:
        record = await registry.create(
            KeyCreate(provider=Provider.OPENAI, models=["m"], key="k4")
        )
        await registry.mark_cooldown(record.id)
        ttl = await registry.redis.ttl(_state_key(record.id))
        assert 0 < ttl <= 2
        assert record.id not in [r.id for r in await registry.routable_keys()]
    finally:
        get_settings.cache_clear()


# -- boundary math — planted anchors ±5 s, incl. the public validated path ---


@pytest.mark.asyncio
async def test_mark_exhausted_rolls_forward_past_anchor(registry: PoolRegistry):
    record = await registry.create(KeyCreate(provider=Provider.OPENAI, models=["m"], key="k5"))

    now = datetime.now(UTC)
    past_anchor = now - timedelta(hours=2)
    await registry.redis.hset(
        _record_key(record.id),
        mapping={"reset_at": past_anchor.isoformat(), "reset_period_hours": "24"},
    )

    await registry.mark_exhausted(record.id)

    ttl = await registry.redis.ttl(_state_key(record.id))
    assert abs(ttl - 22 * 3600) <= 5  # boundary rolls to anchor + 24 h


@pytest.mark.asyncio
async def test_mark_exhausted_custom_period_boundary_math(registry: PoolRegistry):
    record = await registry.create(KeyCreate(provider=Provider.OPENAI, models=["m"], key="k6"))

    now = datetime.now(UTC)
    anchor = now - timedelta(hours=71)
    await registry.redis.hset(
        _record_key(record.id),
        mapping={"reset_at": anchor.isoformat(), "reset_period_hours": "72"},
    )

    await registry.mark_exhausted(record.id)

    ttl = await registry.redis.ttl(_state_key(record.id))
    assert abs(ttl - 3600) <= 5  # boundary = anchor + 72 h = now + 1 h


@pytest.mark.asyncio
async def test_mark_exhausted_custom_future_anchor_via_public_api(registry: PoolRegistry):
    record = await registry.create(
        KeyCreate(
            provider=Provider.OPENAI,
            models=["m"],
            key="k7",
            reset_at=datetime.now(UTC) + timedelta(hours=3),
        )
    )

    await registry.mark_exhausted(record.id)

    ttl = await registry.redis.ttl(_state_key(record.id))
    assert abs(ttl - 3 * 3600) <= 5


# -- Marker-overwrite safety -----------------------------------------------------


@pytest.mark.asyncio
async def test_shorter_marker_never_clobbers_longer_exclusion(registry: PoolRegistry):
    record = await registry.create(KeyCreate(provider=Provider.OPENAI, models=["m"], key="k11"))

    await registry.mark_exhausted(record.id)  # excluded until next reset (long)
    ttl_before = await registry.redis.ttl(_state_key(record.id))

    await registry.mark_cooldown(record.id)  # 60 s — must NOT shorten the exclusion

    assert await registry.effective_state(record.id) == KeyState.EXHAUSTED
    ttl_after = await registry.redis.ttl(_state_key(record.id))
    assert abs(ttl_after - ttl_before) <= 2

    # The reverse direction is legitimate: a longer marker replaces a shorter one.
    record2 = await registry.create(KeyCreate(provider=Provider.OPENAI, models=["m"], key="k12"))
    await registry.mark_cooldown(record2.id)
    await registry.mark_exhausted(record2.id)
    assert await registry.effective_state(record2.id) == KeyState.EXHAUSTED


# -- TTL expiry -> automatic return to active (no sleeping in tests) --------


@pytest.mark.asyncio
async def test_marker_expiry_returns_to_active(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    import time as time_module

    from app.config import get_settings

    monkeypatch.setenv("PALIMPSEST_COOLDOWN_SECONDS", "2")
    get_settings.cache_clear()
    try:
        record = await registry.create(
            KeyCreate(provider=Provider.OPENAI, models=["m"], key="k8")
        )
        await registry.mark_cooldown(record.id)
        assert await registry.effective_state(record.id) == KeyState.COOLDOWN

        # fakeredis stamps its expiry clock from time.time() on every command; advance
        # it instead of sleeping (this fakeredis release has no push_time helper).
        # Scoped to the final assertions via a context so nothing else runs under it.
        with monkeypatch.context() as m:
            future = time_module.time() + 100
            m.setattr(time_module, "time", lambda: future)

            assert await registry.effective_state(record.id) == KeyState.ACTIVE
            assert record.id in [r.id for r in await registry.routable_keys()]
    finally:
        get_settings.cache_clear()


@pytest.mark.asyncio
async def test_marker_expires_during_suspension_resume_returns_active(
    registry: PoolRegistry, monkeypatch: pytest.MonkeyPatch
):
    import time as time_module

    from app.config import get_settings

    monkeypatch.setenv("PALIMPSEST_COOLDOWN_SECONDS", "2")
    get_settings.cache_clear()
    try:
        record = await registry.create(
            KeyCreate(provider=Provider.OPENAI, models=["m"], key="k8b")
        )
        await registry.mark_cooldown(record.id)
        await registry.suspend(record.id)
        assert await registry.effective_state(record.id) == KeyState.SUSPENDED

        with monkeypatch.context() as m:
            future = time_module.time() + 100
            m.setattr(time_module, "time", lambda: future)
            # While suspended the key stays suspended even though the marker expired.
            assert await registry.effective_state(record.id) == KeyState.SUSPENDED

        resumed = await registry.resume(record.id)
        assert resumed.state == KeyState.ACTIVE
    finally:
        get_settings.cache_clear()


# -- covered at the API level (tests/test_pool_api.py) — realm auth. --------


# -- remove clears everything; digest freed --------------------------------


@pytest.mark.asyncio
async def test_remove_clears_everything_and_frees_digest(registry: PoolRegistry):
    record = await registry.create(KeyCreate(provider=Provider.OPENAI, models=["m"], key="k9"))
    await registry.mark_exhausted(record.id)

    await registry.remove(record.id)

    assert await registry.get(record.id) is None
    with pytest.raises(KeyNotFoundError):
        await registry.effective_state(record.id)
    with pytest.raises(KeyNotFoundError):
        await registry.get_key_material(record.id)

    # Digest slot freed — identical raw material can be re-added.
    record2 = await registry.create(KeyCreate(provider=Provider.OPENAI, models=["m"], key="k9"))
    assert record2.id != record.id


@pytest.mark.asyncio
async def test_remove_unknown_key_raises(registry: PoolRegistry):
    with pytest.raises(KeyNotFoundError):
        await registry.remove("does-not-exist")


@pytest.mark.asyncio
async def test_suspend_unknown_key_raises(registry: PoolRegistry):
    with pytest.raises(KeyNotFoundError):
        await registry.suspend("does-not-exist")


@pytest.mark.asyncio
async def test_resume_unknown_key_raises(registry: PoolRegistry):
    with pytest.raises(KeyNotFoundError):
        await registry.resume("does-not-exist")


@pytest.mark.asyncio
async def test_mark_exhausted_unknown_key_raises(registry: PoolRegistry):
    with pytest.raises(KeyNotFoundError):
        await registry.mark_exhausted("does-not-exist")


@pytest.mark.asyncio
async def test_mark_cooldown_unknown_key_raises(registry: PoolRegistry):
    with pytest.raises(KeyNotFoundError):
        await registry.mark_cooldown("does-not-exist")


# -- Corrupt/partial records: reads skip, never abort -----------------------------


@pytest.mark.asyncio
async def test_corrupt_record_is_skipped_not_fatal(registry: PoolRegistry):
    good = await registry.create(KeyCreate(provider=Provider.OPENAI, models=["m"], key="good"))

    await registry.redis.sadd("pgw:pool:keys", "corrupt-id")
    await registry.redis.hset("pgw:pool:keys:corrupt-id", mapping={"id": "corrupt-id"})

    listed = await registry.list()
    assert [r.id for r in listed] == [good.id]
    assert await registry.get("corrupt-id") is None
    assert [r.id for r in await registry.routable_keys()] == [good.id]


# -- sole decryption point + round trip -------------------------------------


@pytest.mark.asyncio
async def test_get_key_material_round_trip(registry: PoolRegistry):
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["m"], key="raw-material-xyz")
    )

    assert await registry.get_key_material(record.id) == "raw-material-xyz"


@pytest.mark.asyncio
async def test_undecryptable_material_raises_key_not_found(registry: PoolRegistry):
    from cryptography.fernet import Fernet

    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["m"], key="rotated-material")
    )
    # A valid Fernet blob encrypted under a DIFFERENT key (e.g. rotated master key).
    foreign = Fernet(Fernet.generate_key()).encrypt(b"other-material").decode()
    await registry.redis.hset(_record_key(record.id), mapping={"key_enc": foreign})

    with pytest.raises(KeyNotFoundError):
        await registry.get_key_material(record.id)


@pytest.mark.asyncio
async def test_list_states_reports_marker_ttl(registry: PoolRegistry):
    record = await registry.create(
        KeyCreate(provider=Provider.OPENAI, models=["m"], key="k10")
    )
    await registry.mark_exhausted(record.id)

    views = await registry.list_states()
    view = next(v for v in views if v.id == record.id)

    assert view.state == KeyState.EXHAUSTED
    assert view.seconds_remaining is not None
    assert view.seconds_remaining > 0
