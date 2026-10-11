"""Provider rate-limit info: observed response headers and OpenRouter's live view."""
import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

import app.provider.adapter as adapter
from app.main import app
from app.pool import limits
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def operator_headers(redis):
    result = await OperatorStore(redis).create_key()
    return {"Authorization": f"Bearer {result['key']}"}


async def _key(redis, provider=Provider.OPENROUTER, material="sk-or-secret-material") -> str:
    record = await PoolRegistry(redis).create(
        KeyCreate(provider=provider, models=["m"], key=material)
    )
    return record.id


def test_only_rate_limit_headers_are_kept_and_prefix_is_stripped():
    kept = limits.rate_limit_headers(
        {
            "llm_provider-x-ratelimit-remaining-requests": "7",
            "x-ratelimit-reset": "1700000000",
            "anthropic-ratelimit-tokens-remaining": "1000",
            "Retry-After": "30",
            "llm_provider-set-cookie": "session=abc",
            "authorization": "Bearer sk-secret",
            "content-type": "application/json",
        }
    )
    assert kept == {
        "x-ratelimit-remaining-requests": "7",
        "x-ratelimit-reset": "1700000000",
        "anthropic-ratelimit-tokens-remaining": "1000",
        "retry-after": "30",
    }


@pytest.mark.asyncio
async def test_a_real_attempt_records_the_headers_it_got(redis, monkeypatch):
    class _Response:
        _hidden_params = {
            "additional_headers": {"llm_provider-x-ratelimit-remaining-requests": "42"}
        }

    async def fake_acompletion(**kwargs):
        return _Response()

    monkeypatch.setattr(adapter, "acompletion", fake_acompletion)
    key_id = await _key(redis, Provider.OPENAI)

    result = await adapter.attempt(PoolRegistry(redis), key_id, "m", [{"role": "user", "content": "x"}])

    assert result.ok
    observed = await limits.get_observed(redis, key_id)
    assert observed["headers"] == {"x-ratelimit-remaining-requests": "42"}
    assert observed["updated_at"]


@pytest.mark.asyncio
async def test_a_rate_limited_attempt_records_retry_after(redis, monkeypatch):
    from litellm import RateLimitError

    async def fake_acompletion(**kwargs):
        response = httpx.Response(
            429,
            headers={"retry-after": "12", "x-ratelimit-remaining-requests": "0"},
            request=httpx.Request("POST", "https://x"),
        )
        raise RateLimitError("slow down", llm_provider="openai", model="m", response=response)

    monkeypatch.setattr(adapter, "acompletion", fake_acompletion)
    key_id = await _key(redis, Provider.OPENAI)

    result = await adapter.attempt(PoolRegistry(redis), key_id, "m", [{"role": "user", "content": "x"}])

    assert not result.ok
    observed = await limits.get_observed(redis, key_id)
    assert observed["headers"] == {"retry-after": "12", "x-ratelimit-remaining-requests": "0"}


@pytest.mark.asyncio
async def test_openrouter_live_view_keeps_only_whitelisted_numbers(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers["authorization"]
        seen["url"] = str(request.url)
        return httpx.Response(
            200,
            json={
                "data": {
                    "label": "sk-or-v1-abc...xyz",
                    "is_free_tier": True,
                    "limit": None,
                    "limit_remaining": None,
                    "usage": 0.5,
                    "free_model_daily_requests": {
                        "used": 3, "limit": 50, "remaining": 47, "internal": "x",
                    },
                }
            },
        )

    real = httpx.AsyncClient
    monkeypatch.setattr(limits.httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(handler)))

    live = await limits.fetch_openrouter("sk-or-secret", "https://openrouter.ai/api/v1")

    assert seen == {"auth": "Bearer sk-or-secret", "url": "https://openrouter.ai/api/v1/key"}
    assert "label" not in live
    assert live["is_free_tier"] is True
    assert live["free_model_daily_requests"] == {"used": 3, "limit": 50, "remaining": 47}


@pytest.mark.asyncio
async def test_openrouter_failure_is_reported_without_leaking(monkeypatch):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        limits.httpx,
        "AsyncClient",
        lambda *a, **k: real(transport=httpx.MockTransport(lambda r: httpx.Response(401, text="sk-or-secret bad"))),
    )
    with pytest.raises(limits.LiveLimitsError) as err:
        await limits.fetch_openrouter("sk-or-secret", "https://openrouter.ai/api/v1")
    assert str(err.value) == "OpenRouter answered 401 for this key."


@pytest.mark.asyncio
async def test_endpoint_combines_live_and_observed_for_openrouter(
    client, redis, operator_headers, monkeypatch
):

    async def fake_fetch(api_key, base_url):
        assert api_key == "sk-or-secret-material"
        return {"is_free_tier": True, "free_model_daily_requests": {"used": 1, "limit": 50, "remaining": 49}}

    monkeypatch.setattr(limits, "fetch_openrouter", fake_fetch)
    key_id = await _key(redis)
    await limits.record_observed(redis, key_id, {"x-ratelimit-limit": "20"})

    resp = await client.get(f"/v1/operator/pool/keys/{key_id}/provider-limits", headers=operator_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["provider"] == "openrouter" and body["live_supported"] is True
    assert body["live"]["free_model_daily_requests"]["remaining"] == 49
    assert body["live_error"] is None
    assert body["observed"]["headers"] == {"x-ratelimit-limit": "20"}
    assert "sk-or-secret" not in resp.text


@pytest.mark.asyncio
async def test_endpoint_for_a_provider_without_a_live_api_shows_observed_only(
    client, redis, operator_headers
):
    key_id = await _key(redis, Provider.OPENAI, "sk-openai-secret")

    body = (
        await client.get(f"/v1/operator/pool/keys/{key_id}/provider-limits", headers=operator_headers)
    ).json()

    assert body["live_supported"] is False and body["live"] is None and body["observed"] is None


@pytest.mark.asyncio
async def test_endpoint_surfaces_a_live_failure_as_a_message(
    client, redis, operator_headers, monkeypatch
):

    async def failing(api_key, base_url):
        raise limits.LiveLimitsError("Could not reach OpenRouter.")

    monkeypatch.setattr(limits, "fetch_openrouter", failing)
    key_id = await _key(redis)

    resp = await client.get(f"/v1/operator/pool/keys/{key_id}/provider-limits", headers=operator_headers)

    assert resp.status_code == 200
    assert resp.json()["live"] is None
    assert resp.json()["live_error"] == "Could not reach OpenRouter."


@pytest.mark.asyncio
async def test_endpoint_unknown_key_and_auth(client, redis, operator_headers):
    key_id = await _key(redis)
    path = f"/v1/operator/pool/keys/{key_id}/provider-limits"

    assert (await client.get("/v1/operator/pool/keys/nope/provider-limits", headers=operator_headers)).status_code == 404
    assert (await client.get(path)).status_code == 401


@pytest.mark.asyncio
async def test_removing_a_key_forgets_its_observed_limits(redis):
    key_id = await _key(redis)
    await limits.record_observed(redis, key_id, {"x-ratelimit-limit": "20"})

    await PoolRegistry(redis).remove(key_id)

    assert await limits.get_observed(redis, key_id) is None
