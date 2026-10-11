"""Provider model catalogues: listing, parsing, and the operator routes."""
import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.pool import catalog
from app.pool.models import KeyCreate, Provider
from app.pool.registry import PoolRegistry
from app.store.operator import OperatorStore
from app.store.redis_store import RedisStore


def _serve(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        catalog.httpx,
        "AsyncClient",
        lambda *a, **k: real(transport=httpx.MockTransport(handler)),
    )


@pytest.mark.asyncio
async def test_openrouter_catalogue_flags_free_models_and_sorts(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "z/paid", "name": "Paid"},
                    {"id": "a/model:free", "name": "Free one"},
                    {"id": "a/model:free", "name": "dupe"},
                    {"id": ""},
                ]
            },
        )

    _serve(monkeypatch, handler)
    models = await catalog.fetch_models(Provider.OPENROUTER, "sk-or-x")

    assert seen == {"url": "https://openrouter.ai/api/v1/models", "auth": "Bearer sk-or-x"}
    assert [m["id"] for m in models] == ["a/model:free", "z/paid"]
    assert models[0]["free"] is True and models[1]["free"] is False
    assert models[0]["name"] == "Free one"


@pytest.mark.asyncio
async def test_openai_uses_bearer_and_anthropic_uses_its_own_headers(monkeypatch):
    calls = []

    def handler(request):
        calls.append((str(request.url), dict(request.headers)))
        return httpx.Response(200, json={"data": [{"id": "m1", "display_name": "M One"}]})

    _serve(monkeypatch, handler)
    openai = await catalog.fetch_models(Provider.OPENAI, "sk-o")
    anthropic = await catalog.fetch_models(Provider.ANTHROPIC, "sk-ant")

    assert calls[0][0] == "https://api.openai.com/v1/models"
    assert calls[0][1]["authorization"] == "Bearer sk-o"
    assert calls[1][0].startswith("https://api.anthropic.com/v1/models")
    assert calls[1][1]["x-api-key"] == "sk-ant" and calls[1][1]["anthropic-version"]
    assert "authorization" not in calls[1][1]
    assert openai[0] == {"id": "m1", "name": "M One"}
    assert anthropic[0]["name"] == "M One"


@pytest.mark.asyncio
async def test_google_keeps_chat_models_strips_prefix_and_sends_key_in_header(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"], seen["key"] = str(request.url), request.headers["x-goog-api-key"]
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "name": "models/gemini-pro",
                        "displayName": "Gemini Pro",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {"name": "models/embed-1", "supportedGenerationMethods": ["embedContent"]},
                ]
            },
        )

    _serve(monkeypatch, handler)
    models = await catalog.fetch_models(Provider.GOOGLE, "g-key")

    assert [m["id"] for m in models] == ["gemini-pro"]
    assert "g-key" not in seen["url"] and seen["key"] == "g-key"  # never in the URL


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "refused this key"), (403, "refused this key"), (500, "answered 500")],
)
async def test_provider_errors_become_safe_messages(monkeypatch, status, expected):
    _serve(monkeypatch, lambda r: httpx.Response(status, text="sk-secret leaked"))
    with pytest.raises(catalog.CatalogError) as err:
        await catalog.fetch_models(Provider.OPENAI, "sk-secret")
    assert expected in str(err.value) and "sk-secret" not in str(err.value)


@pytest.mark.asyncio
async def test_unreachable_provider_is_a_safe_message(monkeypatch):
    def handler(request):
        raise httpx.ConnectError("boom sk-secret")

    _serve(monkeypatch, handler)
    with pytest.raises(catalog.CatalogError) as err:
        await catalog.fetch_models(Provider.OPENROUTER, "sk-secret")
    assert "sk-secret" not in str(err.value)


# -- operator routes ------------------------------------------------------------------


@pytest.fixture
async def redis():
    return FakeRedis()


@pytest.fixture
async def client(redis):
    app.state.store = RedisStore(redis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


@pytest.fixture
async def headers(redis):
    return {"Authorization": f"Bearer {(await OperatorStore(redis).create_key())['key']}"}


@pytest.mark.asyncio
async def test_existing_key_route_uses_its_stored_key_and_never_returns_it(
    client, redis, headers, monkeypatch
):
    key_id = (
        await PoolRegistry(redis).create(
            KeyCreate(provider=Provider.OPENROUTER, models=["a"], key="sk-or-stored")
        )
    ).id
    seen = {}

    async def fake(provider, api_key):
        seen.update(provider=provider, key=api_key)
        return [{"id": "m1", "name": None, "free": False}]

    monkeypatch.setattr(catalog, "fetch_models", fake)
    resp = await client.get(f"/v1/operator/pool/keys/{key_id}/available-models", headers=headers)

    assert resp.status_code == 200
    assert resp.json() == {"object": "list", "data": [{"id": "m1", "name": None, "free": False}]}
    assert seen == {"provider": Provider.OPENROUTER, "key": "sk-or-stored"}
    assert "sk-or-stored" not in resp.text


@pytest.mark.asyncio
async def test_new_key_route_lists_with_the_pasted_key(client, headers, monkeypatch):
    import app.api.pool_routes as routes

    async def fake(provider, api_key):
        assert (provider, api_key) == (Provider.OPENAI, "sk-typed")
        return [{"id": "gpt-4o-mini", "name": None}]

    monkeypatch.setattr(routes, "fetch_models", fake)
    resp = await client.post(
        "/v1/operator/pool/available-models",
        headers=headers,
        json={"provider": "openai", "key": " sk-typed "},
    )

    assert resp.status_code == 200 and resp.json()["data"][0]["id"] == "gpt-4o-mini"
    assert "sk-typed" not in resp.text


@pytest.mark.asyncio
async def test_provider_failure_is_502_not_a_signout(client, headers, monkeypatch):
    import app.api.pool_routes as routes

    async def refused(provider, api_key):
        raise catalog.CatalogError("openai refused this key when listing models.")

    monkeypatch.setattr(routes, "fetch_models", refused)
    resp = await client.post(
        "/v1/operator/pool/available-models",
        headers=headers,
        json={"provider": "openai", "key": "sk-bad"},
    )

    # 401/403 from a provider must not look like a rejected operator credential.
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "provider_catalog_unavailable"


@pytest.mark.asyncio
async def test_routes_need_operator_auth_and_known_key(client, redis, headers):
    body = {"provider": "openai", "key": "k"}
    assert (await client.post("/v1/operator/pool/available-models", json=body)).status_code == 401
    assert (await client.get("/v1/operator/pool/keys/x/available-models")).status_code == 401
    unknown = await client.get("/v1/operator/pool/keys/nope/available-models", headers=headers)
    assert unknown.status_code == 404
    bad = await client.post(
        "/v1/operator/pool/available-models",
        headers=headers,
        json={"provider": "nope", "key": "k"},
    )
    assert bad.status_code == 422
