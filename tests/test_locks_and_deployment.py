"""Session locks, history caps, Redis error mapping, Compose defaults and CORS."""
import asyncio
import pathlib

import pytest
from fakeredis.aioredis import FakeRedis

from app.config import Settings
from app.context.rebuild import SessionRef, rebuild
from app.session import policy
from app.session.locks import SessionLock
from app.session.policy import (
    SessionBusy,
    TurnLock,
    acquire_turn_lock,
    release_turn_lock,
)
from app.session.store import SessionStore


@pytest.fixture
async def redis():
    return FakeRedis()


# -- rebuild never raises into the failover loop ------------------------------


def _session(redis) -> SessionRef:
    return SessionRef(account_id="a", session_id="s", turn_count=2, redis=redis)


@pytest.mark.asyncio
async def test_redis_error_degrades_to_current_prompt(redis, monkeypatch):
    async def boom(*a, **kw):
        raise RuntimeError("redis blew up")

    monkeypatch.setattr(SessionStore, "read_recent", boom)

    out = await rebuild([{"role": "user", "content": "hi"}], _session(redis), 1)
    assert out == [{"role": "user", "content": "hi"}]


# -- session store ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_read_recent_is_capped_by_the_stores_own_max_exchanges(redis):
    store = SessionStore(redis, max_exchanges=3)
    await store.create_or_claim("a", "s")
    for i in range(5):
        await store.append_exchange("a", "s", [{"role": "user", "content": str(i)}])
    recent = await store.read_recent("a", "s", count=100)
    assert [e["messages"][0]["content"] for e in recent] == ["2", "3", "4"]


def test_max_exchanges_must_be_positive(redis):
    with pytest.raises(ValueError):
        SessionStore(redis, max_exchanges=0)


# -- lock policy -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_session_busy_retry_after_is_the_holders_remaining_ttl(redis):
    store = SessionStore(redis)
    lock_key = store._lock_key("a", "s")
    held, _ = await SessionLock(redis, lock_ttl_ms=10_000).acquire(lock_key)
    assert held
    with pytest.raises(SessionBusy) as exc:
        await acquire_turn_lock(redis, "a", "s", wait_ms=50, poll_ms=10)
    assert exc.value.retry_after_ms != 50  # not the wait budget
    assert 5_000 < exc.value.retry_after_ms <= 10_000


@pytest.mark.asyncio
async def test_renewal_keeps_the_lock_past_its_ttl_and_release_frees_it(redis, monkeypatch):
    monkeypatch.setattr(policy, "SessionLock", lambda r: SessionLock(r, lock_ttl_ms=300))
    turn = await acquire_turn_lock(redis, "a", "s")
    lock_key = turn.lock_key
    try:
        await asyncio.sleep(0.8)  # > 2x TTL: only the renew loop keeps the key alive
        assert await redis.exists(lock_key)
        other, _ = await SessionLock(redis, lock_ttl_ms=300).acquire(lock_key)
        assert not other
    finally:
        await release_turn_lock(turn)
    assert turn.renew_task.done()
    assert not await redis.exists(lock_key)


@pytest.mark.asyncio
async def test_release_survives_a_renew_task_that_died_with_an_error(redis):
    lock = SessionLock(redis, lock_ttl_ms=10_000)
    held, token = await lock.acquire("k")
    assert held

    async def dies():
        raise RuntimeError("renew crashed")

    task = asyncio.create_task(dies())
    await asyncio.sleep(0)
    await release_turn_lock(TurnLock(lock=lock, lock_key="k", token=token, renew_task=task))
    assert not await redis.exists("k")  # lock still released


# -- pool routes: Redis outage -> 503 --------------------------------------------


@pytest.mark.asyncio
async def test_pool_call_maps_redis_py_errors_to_503():
    import redis.exceptions as rex

    from app.api.pool_routes import _pool_call
    from app.errors import GatewayError

    async def down():
        raise rex.ConnectionError("redis down")

    with pytest.raises(GatewayError) as exc:
        await _pool_call(down)
    assert exc.value.status_code == 503 and exc.value.code == "pool_unavailable"

    async def bug():
        raise KeyError("programming error")

    with pytest.raises(KeyError):  # real bugs are never masked
        await _pool_call(bug)


# -- deployment ------------------------------------------------------------------


def _compose() -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(pathlib.Path("docker-compose.yml").read_text(encoding="utf-8"))


def _env(service: dict) -> dict[str, str]:
    """Normalise a compose ``environment`` block, list or mapping form, to a dict."""
    raw = service.get("environment") or {}
    if isinstance(raw, dict):
        return {str(k): "" if v is None else str(v) for k, v in raw.items()}
    out = {}
    for item in raw:
        key, _, value = str(item).partition("=")
        out[key] = value
    return out


def test_compose_generates_its_own_secrets_instead_of_demanding_them():
    """No `:?` on the secrets: the entrypoint generates them, so the stack boots
    on a fresh clone with no .env at all. Demanding them here would put a manual
    key-generation step back in front of every new user."""
    env = _env(_compose()["services"]["app"])
    for name in ("PALIMPSEST_GATE_MASTER_KEY", "PALIMPSEST_GATE_OPERATOR_KEY"):
        assert name in env, f"{name} must still be passed through from the environment"
        assert ":?" not in env[name], f"{name} must not be required by compose"
        assert ":-" in env[name], f"{name} must default to empty so the entrypoint can fill it"


def test_compose_keeps_generated_secrets_in_a_persistent_volume():
    """The generated master key is the root of trust for every stored credential.
    Losing it makes the whole pool undecryptable, so it must outlive the container."""
    compose = _compose()
    volumes = [str(v) for v in compose["services"]["app"].get("volumes") or []]
    assert any(v.startswith("gateway_secrets:") for v in volumes), volumes
    assert "gateway_secrets" in (compose.get("volumes") or {})


def test_compose_never_publishes_redis_to_the_host():
    """Redis has no auth and holds encrypted credentials. The app reaches it over
    the compose network, so publishing it would only add risk — and a host bind
    that collides with any locally installed Redis."""
    redis = _compose()["services"]["redis"]
    assert not redis.get("ports"), "redis must not publish a host port"


def test_compose_binds_the_gateway_to_loopback_by_default():
    """The gateway has no TLS and no rate limiting, so the default must not be
    reachable from the network."""
    ports = _compose()["services"]["app"].get("ports") or []
    assert ports, "the gateway must publish a port"
    # The bind is a substitution so it stays overridable; what matters is that
    # its default is loopback.
    assert any(":-127.0.0.1}" in str(p) for p in ports), ports


def test_every_service_declares_a_healthcheck_so_compose_wait_works():
    for name, service in _compose()["services"].items():
        assert service.get("healthcheck"), f"{name} needs a healthcheck for --wait"


def test_app_waits_for_redis_to_be_healthy_before_starting():
    depends = _compose()["services"]["app"].get("depends_on") or {}
    assert depends.get("redis", {}).get("condition") == "service_healthy"


# -- CORS fails closed -------------------------------------------------------------


def test_cors_default_denies_cross_origin():
    """The dashboard is same-origin and non-browser clients ignore CORS, so the
    default must deny rather than allow. A wildcard here would let any page in
    the user's browser issue credentialed requests against the gateway.

    ``_env_file=None`` so this asserts the shipped default, not whatever a
    developer's local .env happens to set."""
    settings = Settings(_env_file=None)
    assert settings.cors_origins == "[]"
    assert settings.cors_origin_list == []


@pytest.mark.parametrize(
    "raw",
    [
        "",  # unset / empty
        "   ",  # whitespace
        "http://localhost:3000",  # valid origin but not valid JSON — the classic typo
        "[",  # truncated JSON
        "null",
        "0",
        "{}",
        '["*"]x',  # trailing garbage
    ],
)
def test_malformed_cors_never_widens_access(raw):
    settings = Settings(cors_origins=raw)
    assert settings.cors_origin_list == [], f"{raw!r} must not widen CORS"


def test_explicit_cors_origins_are_honoured():
    assert Settings(cors_origins='["http://localhost:3000"]').cors_origin_list == [
        "http://localhost:3000"
    ]
    # A bare JSON string is tolerated, for convenience.
    assert Settings(cors_origins='"http://localhost:3000"').cors_origin_list == [
        "http://localhost:3000"
    ]
