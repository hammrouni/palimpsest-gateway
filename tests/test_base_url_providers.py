"""Per-key base URLs (custom endpoints) and the Z.ai and GitHub Models providers."""
import os

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from httpx import AsyncClient
from pydantic import ValidationError

import app.provider.adapter as adapter_module
from app.config import get_settings
from app.pool import catalog
from app.pool.models import KeyCreate, KeyUpdate, Provider, clean_base_url
from app.pool.registry import PoolRegistry
from app.provider.adapter import attempt
from app.store.operator import OperatorStore

KEYS = "/v1/operator/pool/keys"
MESSAGES = [{"role": "user", "content": "hi"}]


@pytest.fixture(autouse=True)
def _master_key():
    if not os.environ.get("PALIMPSEST_GATE_MASTER_KEY"):
        from cryptography.fernet import Fernet

        os.environ["PALIMPSEST_GATE_MASTER_KEY"] = Fernet.generate_key().decode()


@pytest.fixture
async def registry():
    return PoolRegistry(FakeRedis())


@pytest.fixture
async def operator_headers(fake_redis: FakeRedis) -> dict[str, str]:
    result = await OperatorStore(fake_redis).create_key()
    return {"Authorization": f"Bearer {result['key']}"}


def _serve(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        catalog.httpx, "AsyncClient", lambda *a, **k: real(transport=httpx.MockTransport(handler))
    )


async def _calls(monkeypatch, registry, key_id, model):
    seen = []

    async def fake(**kwargs):
        seen.append(kwargs)
        return type("R", (), {"id": "x"})()

    monkeypatch.setattr(adapter_module, "acompletion", fake)
    result = await attempt(registry, key_id, model, MESSAGES)
    assert result.ok
    return seen[0]


# -- validation -----------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, cleaned",
    [
        ("https://llm.example.com/v1", "https://llm.example.com/v1"),
        ("  https://llm.example.com/v1/  ", "https://llm.example.com/v1"),
        ("http://localhost:11434/v1", "http://localhost:11434/v1"),
        ("http://192.168.1.20:8000/v1", "http://192.168.1.20:8000/v1"),
    ],
)
def test_clean_base_url_accepts_model_servers(raw, cleaned):
    assert clean_base_url(raw) == cleaned


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "llm.example.com/v1",
        "ftp://llm.example.com",
        "https://user:pass@llm.example.com/v1",
        "https://llm.example.com/v1?key=1",
        "https://llm.example.com/v1#frag",
        "http://169.254.169.254/latest/meta-data",
        "http://metadata.google.internal/computeMetadata/v1",
        "http://[fd00:ec2::254]/",
        "http://llm.example.com:99999/v1",
        "https://" + "a" * 300 + ".com",
    ],
)
def test_clean_base_url_rejects_unsafe_or_malformed(raw):
    with pytest.raises(ValueError):
        clean_base_url(raw)


def test_custom_key_needs_a_base_url_unless_one_is_configured(monkeypatch):
    monkeypatch.setattr(get_settings(), "custom_base_url", None)
    with pytest.raises(ValidationError, match="base_url"):
        KeyCreate(provider=Provider.CUSTOM, models=["m"], key="k")

    monkeypatch.setattr(get_settings(), "custom_base_url", "https://default.example.com/v1")
    assert KeyCreate(provider=Provider.CUSTOM, models=["m"], key="k").base_url is None


def test_base_url_is_refused_for_providers_with_their_own_host():
    with pytest.raises(ValidationError, match="do not take a base_url"):
        KeyCreate(
            provider=Provider.ANTHROPIC, models=["m"], key="k", base_url="https://x.example.com"
        )


def test_update_can_clear_the_base_url():
    update = KeyUpdate(base_url=None)
    assert "base_url" in update.model_fields_set and update.base_url is None


# -- the key's own endpoint is what the request uses ----------------------------


@pytest.mark.asyncio
async def test_custom_key_sends_requests_to_its_own_base_url(registry, monkeypatch):
    record = await registry.create(
        KeyCreate(
            provider=Provider.CUSTOM, models=["llama3"], key="k1",
            base_url="http://localhost:11434/v1/",
        )
    )
    assert record.base_url == "http://localhost:11434/v1"
    call = await _calls(monkeypatch, registry, record.id, "llama3")
    assert call["api_base"] == "http://localhost:11434/v1"
    assert call["model"] == "openai/llama3"


@pytest.mark.asyncio
async def test_two_custom_keys_keep_separate_endpoints(registry, monkeypatch):
    a = await registry.create(
        KeyCreate(provider=Provider.CUSTOM, models=["m"], key="ka", base_url="https://a.example.com/v1")
    )
    b = await registry.create(
        KeyCreate(provider=Provider.CUSTOM, models=["m"], key="kb", base_url="https://b.example.com/v1")
    )
    assert (await _calls(monkeypatch, registry, a.id, "m"))["api_base"] == "https://a.example.com/v1"
    assert (await _calls(monkeypatch, registry, b.id, "m"))["api_base"] == "https://b.example.com/v1"


@pytest.mark.asyncio
async def test_zai_and_github_route_through_their_litellm_prefix(registry, monkeypatch):
    zai = await registry.create(KeyCreate(provider=Provider.ZAI, models=["glm-4.6"], key="kz"))
    github = await registry.create(
        KeyCreate(provider=Provider.GITHUB, models=["openai/gpt-4.1"], key="kg")
    )
    zai_call = await _calls(monkeypatch, registry, zai.id, "glm-4.6")
    assert zai_call["model"] == "zai/glm-4.6"
    assert zai_call["api_base"] is None  # litellm's default is the Z.ai API

    github_call = await _calls(monkeypatch, registry, github.id, "openai/gpt-4.1")
    assert github_call["model"] == "github/openai/gpt-4.1"
    assert github_call["api_base"] == "https://models.github.ai/inference"


@pytest.mark.asyncio
async def test_a_key_can_override_a_providers_default_endpoint(registry, monkeypatch):
    record = await registry.create(
        KeyCreate(
            provider=Provider.ZAI, models=["glm-4.6"], key="kz",
            base_url="https://api.z.ai/api/coding/paas/v4",
        )
    )
    call = await _calls(monkeypatch, registry, record.id, "glm-4.6")
    assert call["api_base"] == "https://api.z.ai/api/coding/paas/v4"


@pytest.mark.asyncio
async def test_registry_update_sets_and_clears_the_base_url(registry, monkeypatch):
    monkeypatch.setattr(get_settings(), "custom_base_url", None)
    record = await registry.create(
        KeyCreate(provider=Provider.GROQ, models=["m"], key="kq")
    )
    updated = await registry.update(record.id, KeyUpdate(base_url="https://groq.example.com/v1"))
    assert updated.base_url == "https://groq.example.com/v1"
    assert (await registry.update(record.id, KeyUpdate(base_url=None))).base_url is None

    custom = await registry.create(
        KeyCreate(provider=Provider.CUSTOM, models=["m"], key="kc", base_url="https://c.example.com/v1")
    )
    with pytest.raises(ValueError, match="needs a base_url"):
        await registry.update(custom.id, KeyUpdate(base_url=None))

    anthropic = await registry.create(KeyCreate(provider=Provider.ANTHROPIC, models=["m"], key="kn"))
    with pytest.raises(ValueError, match="do not take a base_url"):
        await registry.update(anthropic.id, KeyUpdate(base_url="https://x.example.com/v1"))


# -- the operator API -------------------------------------------------------------


@pytest.mark.asyncio
async def test_api_adds_a_custom_key_and_returns_its_base_url(
    async_client: AsyncClient, operator_headers: dict[str, str], monkeypatch
):
    monkeypatch.setattr(get_settings(), "custom_base_url", None)

    missing = await async_client.post(
        KEYS, headers=operator_headers, json={"provider": "custom", "models": ["m"], "key": "k-x"}
    )
    assert missing.status_code == 422

    added = await async_client.post(
        KEYS,
        headers=operator_headers,
        json={"provider": "custom", "models": ["m"], "key": "k-x",
              "base_url": "https://llm.example.com/v1/"},
    )
    assert added.status_code == 201
    assert added.json()["base_url"] == "https://llm.example.com/v1"

    changed = await async_client.patch(
        f"{KEYS}/{added.json()['id']}", headers=operator_headers,
        json={"base_url": "https://other.example.com/v1"},
    )
    assert changed.status_code == 200 and changed.json()["base_url"] == "https://other.example.com/v1"

    cleared = await async_client.patch(
        f"{KEYS}/{added.json()['id']}", headers=operator_headers, json={"base_url": None}
    )
    assert cleared.status_code == 422 and cleared.json()["error"]["param"] == "base_url"


@pytest.mark.asyncio
async def test_api_accepts_the_new_providers(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    for provider in ("zai", "xai", "github"):
        resp = await async_client.post(
            KEYS, headers=operator_headers,
            json={"provider": provider, "models": ["m"], "key": f"key-{provider}"},
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["provider"] == provider


# -- model catalogues -------------------------------------------------------------


@pytest.mark.asyncio
async def test_custom_catalogue_lists_from_the_given_base_url(monkeypatch):
    monkeypatch.setattr(get_settings(), "custom_base_url", None)
    seen = {}

    def handler(request):
        seen["url"], seen["auth"] = str(request.url), request.headers["authorization"]
        return httpx.Response(200, json={"data": [{"id": "llama3"}, {"id": "qwen"}]})

    _serve(monkeypatch, handler)
    models = await catalog.fetch_models(Provider.CUSTOM, "sk-c", "http://localhost:11434/v1/")
    assert seen == {"url": "http://localhost:11434/v1/models", "auth": "Bearer sk-c"}
    assert [m["id"] for m in models] == ["llama3", "qwen"]


@pytest.mark.asyncio
async def test_custom_catalogue_without_any_url_says_so(monkeypatch):
    monkeypatch.setattr(get_settings(), "custom_base_url", None)
    with pytest.raises(catalog.CatalogError, match="base URL"):
        await catalog.fetch_models(Provider.CUSTOM, "sk-c")


@pytest.mark.asyncio
async def test_catalogue_refuses_an_unsafe_base_url(monkeypatch):
    with pytest.raises(catalog.CatalogError, match="metadata"):
        await catalog.fetch_models(Provider.CUSTOM, "sk-c", "http://169.254.169.254/v1")


@pytest.mark.asyncio
async def test_zai_catalogue_uses_its_api_base(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "glm-4.6"}]})

    _serve(monkeypatch, handler)
    models = await catalog.fetch_models(Provider.ZAI, "zk")
    assert seen["url"] == "https://api.z.ai/api/paas/v4/models"
    assert models[0]["id"] == "glm-4.6"


@pytest.mark.asyncio
async def test_xai_routes_through_its_litellm_prefix(registry, monkeypatch):
    xai = await registry.create(KeyCreate(provider=Provider.XAI, models=["grok-4"], key="kx"))
    call = await _calls(monkeypatch, registry, xai.id, "grok-4")
    assert call["model"] == "xai/grok-4"
    assert call["api_base"] is None  # litellm's default is the xAI API


@pytest.mark.asyncio
async def test_xai_catalogue_uses_its_api_base(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"] = str(request.url)
        return httpx.Response(200, json={"data": [{"id": "grok-4"}]})

    _serve(monkeypatch, handler)
    models = await catalog.fetch_models(Provider.XAI, "xk")
    assert seen["url"] == "https://api.x.ai/v1/models"
    assert models[0]["id"] == "grok-4"


@pytest.mark.asyncio
async def test_github_catalogue_reads_a_bare_array(monkeypatch):
    seen = {}

    def handler(request):
        seen["url"], seen["headers"] = str(request.url), dict(request.headers)
        return httpx.Response(
            200,
            json=[
                {"id": "openai/gpt-4.1", "name": "OpenAI GPT-4.1"},
                {"id": "meta/llama-3.3-70b-instruct", "name": "Llama 3.3 70B"},
            ],
        )

    _serve(monkeypatch, handler)
    models = await catalog.fetch_models(Provider.GITHUB, "ghp_x")
    assert seen["url"] == "https://models.github.ai/catalog/models"
    assert seen["headers"]["authorization"] == "Bearer ghp_x"
    assert seen["headers"]["accept"] == "application/vnd.github+json"
    assert [m["id"] for m in models] == ["meta/llama-3.3-70b-instruct", "openai/gpt-4.1"]
    assert models[1]["name"] == "OpenAI GPT-4.1"


@pytest.mark.asyncio
async def test_api_says_why_a_base_url_was_refused(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    resp = await async_client.post(
        KEYS, headers=operator_headers,
        json={"provider": "custom", "models": ["m"], "key": "k-meta",
              "base_url": "http://169.254.169.254/v1"},
    )
    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["param"] == "base_url"
    assert "metadata" in error["message"]
    assert "169.254" not in error["message"]  # the submitted value is never echoed

    other = await async_client.post(
        KEYS, headers=operator_headers, json={"provider": "openai", "models": [], "key": "k"}
    )
    assert other.json()["error"]["message"] == "Invalid request."


@pytest.mark.asyncio
async def test_duplicate_says_where_the_existing_key_is(
    async_client: AsyncClient, operator_headers: dict[str, str]
):
    first = await async_client.post(
        KEYS, headers=operator_headers,
        json={"provider": "openai", "models": ["m"], "key": "sk-same-material-1234",
              "base_url": "https://api.aionlabs.ai/v1"},
    )
    assert first.status_code == 201

    # Added again, this time as a custom provider: the usual way a key seems "missing".
    again = await async_client.post(
        KEYS, headers=operator_headers,
        json={"provider": "custom", "models": ["m"], "key": "sk-same-material-1234",
              "base_url": "https://api.aionlabs.ai/v1"},
    )
    assert again.status_code == 409
    message = again.json()["error"]["message"]
    assert "already in the pool" in message
    assert "openai" in message and "api.aionlabs.ai" in message
    assert "1234" in message  # the hint names the entry...
    assert "sk-same-material-1234" not in message  # ...the key itself is never shown

    labelled = await async_client.post(
        KEYS, headers=operator_headers,
        json={"provider": "groq", "models": ["m"], "key": "gsk-labelled-0001", "label": "Main"},
    )
    dup = await async_client.post(
        KEYS, headers=operator_headers,
        json={"provider": "groq", "models": ["m"], "key": "gsk-labelled-0001"},
    )
    assert labelled.status_code == 201 and "'Main'" in dup.json()["error"]["message"]
