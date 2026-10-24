"""Provider failures, failover context, startup key seeding and config refresh.

Uses real litellm exception classes rather than stand-ins.
"""

import asyncio
import logging

import httpx
import litellm
import pytest
from fakeredis.aioredis import FakeRedis

import app.provider.adapter as adapter_module
from app.config import get_settings
from app.context.rebuild import SessionRef, rebuild
from app.pool.models import KeyCreate, KeyState, Provider
from app.pool.registry import PoolRegistry
from app.router.classify import classify
from app.router.failover import SYSTEM_OPERATOR_ID, execute_with_failover
from app.session.store import SessionStore
from app.store.operator import OperatorStore

MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture
async def redis():
    return FakeRedis(decode_responses=True)


@pytest.fixture
async def registry(redis):
    return PoolRegistry(redis)


async def _add(registry, key, provider=Provider.OPENAI, model="m"):
    return (
        await registry.create(KeyCreate(provider=provider, models=[model], key=key))
    ).id


# -- provider auth failures -------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "status"),
    [
        (litellm.AuthenticationError("bad key", llm_provider="openai", model="m"), 401),
        (
            litellm.PermissionDeniedError(
                "denied",
                llm_provider="openai",
                model="m",
                response=httpx.Response(
                    403, request=httpx.Request("POST", "https://x")
                ),
            ),
            403,
        ),
        (litellm.NotFoundError("no such model", llm_provider="openai", model="m"), 404),
    ],
)
async def test_adapter_keeps_the_status_of_litellm_errors_outside_apierror(
    registry, monkeypatch, exc, status
):
    key_id = await _add(registry, "sk-some-key-1234")

    async def failing(**kwargs):
        raise exc

    monkeypatch.setattr(adapter_module, "acompletion", failing)
    result = await adapter_module.attempt(registry, key_id, "m", MESSAGES)

    assert not result.ok
    assert result.error.status_code == status


async def test_revoked_key_is_suspended_and_the_request_rotates_to_a_good_key(
    registry, monkeypatch
):
    bad = await _add(registry, "sk-bad-aaaaaaaa")
    good = await _add(registry, "sk-good-bbbbbbb")
    called = []

    async def fake(**kwargs):
        called.append(kwargs["api_key"])
        if "bad" in kwargs["api_key"]:
            raise litellm.AuthenticationError(
                "invalid key", llm_provider="openai", model="m"
            )
        return "OK"

    monkeypatch.setattr(adapter_module, "acompletion", fake)

    result, trace = await execute_with_failover(registry, "m", MESSAGES)

    assert result.ok and trace.terminal == "success"
    assert trace.attempts[-1].key_id == good
    bad_record = await registry.get(bad)
    assert bad_record.state == KeyState.SUSPENDED
    assert bad_record.last_modified_by == SYSTEM_OPERATOR_ID

    # The suspended key is never tried again.
    called.clear()
    await execute_with_failover(registry, "m", MESSAGES)
    assert called == ["sk-good-bbbbbbb"]


def test_network_errors_without_a_status_stay_transient():
    from app.provider.adapter import ProviderAttemptError

    assert (
        classify(ProviderAttemptError(None, "x", "conn reset", Provider.OPENAI)).state
        == "transient"
    )


# -- adapter wiring ----------------------------------------------------------------


async def test_provider_timeout_is_passed_to_every_call(registry, monkeypatch):
    key_id = await _add(registry, "sk-timeout-key-12")
    seen = {}

    async def fake(**kwargs):
        seen.update(kwargs)
        return "OK"

    monkeypatch.setattr(adapter_module, "acompletion", fake)
    monkeypatch.setattr(get_settings(), "provider_timeout", 12.5)
    monkeypatch.setattr(get_settings(), "provider_connect_timeout", 10.0)
    monkeypatch.setattr(get_settings(), "provider_read_timeout", 60.0)
    await adapter_module.attempt(registry, key_id, "m", MESSAGES)

    assert isinstance(seen["timeout"], httpx.Timeout)
    assert seen["timeout"].connect == 10.0
    assert seen["timeout"].read == 60.0


def test_every_provider_prefix_is_a_route_litellm_knows():
    for provider in Provider:
        model = adapter_module._mapped_model(provider, "some-model")
        _, routed_provider, _, _ = litellm.get_llm_provider(model)
        assert routed_provider == adapter_module._MODEL_PREFIX[provider]


# -- failover context rebuild ------------------------------------------------------


async def _session(redis, exchanges):
    store = SessionStore(redis)
    await store.create_or_claim("acc", "s1")
    for exchange in exchanges:
        await store.append_exchange("acc", "s1", exchange)
    return SessionRef(
        account_id="acc", session_id="s1", turn_count=len(exchanges), redis=redis
    )


SYSTEM = {"role": "system", "content": "You are a pirate."}


async def test_rebuild_keeps_the_system_prompt_and_the_whole_tool_turn(redis):
    session = await _session(
        redis,
        [
            [
                SYSTEM,
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
            ]
        ],
    )
    tool_call = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "t1",
                "type": "function",
                "function": {"name": "w", "arguments": "{}"},
            }
        ],
    }
    tool_result = {"role": "tool", "tool_call_id": "t1", "content": "sunny"}
    inbound = [SYSTEM, {"role": "user", "content": "weather?"}, tool_call, tool_result]

    out = await rebuild(inbound, session, 1)

    assert out == [
        SYSTEM,  # once, at the top — the stored copy is not repeated
        {"role": "user", "content": "q1"},
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "weather?"},
        tool_call,
        tool_result,
    ]


async def test_rebuild_is_stable_across_repeated_failovers_with_a_system_prompt(redis):
    session = await _session(
        redis,
        [
            [
                SYSTEM,
                {"role": "user", "content": "q1"},
                {"role": "assistant", "content": "a1"},
            ]
        ],
    )
    first = await rebuild([SYSTEM, {"role": "user", "content": "now"}], session, 1)
    second = await rebuild(first, session, 2)
    assert second == first


# -- PALIMPSEST_PROVIDER_API_KEY -----------------------------------------------------


async def test_env_provider_key_is_pooled_once_for_the_configured_model(
    redis, monkeypatch
):
    from app.main import seed_provider_key

    settings = get_settings()
    monkeypatch.setattr(settings, "provider_api_key", "sk-env-key-123456")
    monkeypatch.setattr(settings, "provider_name", "openrouter")
    monkeypatch.setattr(settings, "provider_model", "some/model")

    await seed_provider_key(redis)
    await seed_provider_key(redis)  # a restart must not duplicate it

    keys = await PoolRegistry(redis).list()
    assert len(keys) == 1
    assert keys[0].provider == Provider.OPENROUTER
    assert keys[0].models == ["some/model"]
    assert await PoolRegistry(redis).get_key_material(keys[0].id) == "sk-env-key-123456"


async def test_invalid_provider_name_is_logged_not_fatal(redis, monkeypatch, caplog):
    from app.main import seed_provider_key

    settings = get_settings()
    monkeypatch.setattr(settings, "provider_api_key", "sk-env-key-123456")
    monkeypatch.setattr(settings, "provider_name", "not-a-provider")

    with caplog.at_level(logging.ERROR, logger="palimpsest.app"):
        await seed_provider_key(redis)

    assert await PoolRegistry(redis).list() == []
    assert "PALIMPSEST_PROVIDER_NAME" in caplog.text
    assert "sk-env-key-123456" not in caplog.text


async def test_no_env_key_seeds_nothing(redis, monkeypatch):
    from app.main import seed_provider_key

    monkeypatch.setattr(get_settings(), "provider_api_key", "")
    await seed_provider_key(redis)
    assert await PoolRegistry(redis).list() == []


# -- PALIMPSEST_ALLOW_SIGNUP -------------------------------------------------------


async def test_closed_signup_requires_an_operator_key(
    async_client, fake_redis, monkeypatch
):
    monkeypatch.setattr(get_settings(), "allow_signup", True)
    user = (await async_client.post("/v1/accounts", json={})).json()
    user_key = user["keys"][0]["key"]
    operator_key = (await OperatorStore(fake_redis).create_key())["key"]

    monkeypatch.setattr(get_settings(), "allow_signup", False)

    anonymous = await async_client.post("/v1/accounts", json={})
    assert anonymous.status_code == 401
    assert anonymous.json()["error"]["code"] == "invalid_api_key"

    as_user = await async_client.post(
        "/v1/accounts", json={}, headers={"Authorization": f"Bearer {user_key}"}
    )
    assert as_user.status_code == 403

    as_operator = await async_client.post(
        "/v1/accounts", json={}, headers={"Authorization": f"Bearer {operator_key}"}
    )
    assert as_operator.status_code == 201
    assert as_operator.json()["keys"][0]["key"].startswith("pgw_live_")


# -- runtime config reaches every instance ------------------------------------------


async def test_override_written_elsewhere_is_picked_up_by_the_refresh_loop(redis):
    from app.main import refresh_config_overrides
    from app.store.config import ConfigStore

    settings = get_settings()
    before = settings.cooldown_seconds
    try:
        # Another instance PATCHed the config: only Redis knows about it.
        await ConfigStore(redis).set_overrides(
            {"cooldown_seconds": 321}, operator_id="op"
        )
        task = asyncio.create_task(refresh_config_overrides(redis, 0.01))
        for _ in range(100):
            await asyncio.sleep(0.01)
            if settings.cooldown_seconds == 321:
                break
        task.cancel()
        assert settings.cooldown_seconds == 321
    finally:
        settings.cooldown_seconds = before
