"""The model catalogue a provider offers, for the operator to pick from.

The operator chooses which of these a pool key serves; ``GET /v1/models`` then
exposes exactly that choice to users. Listing needs the provider key, which is used
here for one outbound request and neither stored nor logged. The host is the
provider's own, the operator's setting, or the base URL of the key being listed
(validated when it was saved); only the add-key form passes one in directly, and
that route is operator-only.
"""
from __future__ import annotations

from typing import Any

import httpx

from app.config import get_settings
from app.pool.models import DEFAULT_BASE_URLS, Provider, clean_base_url

_OPENAI_DEFAULT_BASE = "https://api.openai.com/v1"
_ANTHROPIC_URL = "https://api.anthropic.com/v1/models?limit=1000"
_GOOGLE_URL = "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1000"
_MISTRAL_BASE = "https://api.mistral.ai/v1"
_GROQ_BASE = "https://api.groq.com/openai/v1"
# GitHub Models lists its catalogue outside the inference path, as a bare JSON array.
_GITHUB_CATALOG_URL = "https://models.github.ai/catalog/models"
_GITHUB_HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
_TIMEOUT = 15.0
_MAX_MODELS = 2000
_MAX_ID = 160


class CatalogError(Exception):
    """Listing failed; ``str()`` is a message that is safe to show an operator."""


def _request(
    provider: Provider, api_key: str, base_url: str | None = None
) -> tuple[str, dict[str, str]]:
    """The listing URL and headers; ``base_url`` is a key's own endpoint, if it has one."""
    settings = get_settings()
    bearer = {"Authorization": f"Bearer {api_key}"}
    if provider == Provider.OPENROUTER:
        return settings.openrouter_base_url.rstrip("/") + "/models", bearer
    if provider == Provider.ANTHROPIC:
        return _ANTHROPIC_URL, {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
    if provider == Provider.GOOGLE:
        return _GOOGLE_URL, {"x-goog-api-key": api_key}
    if provider == Provider.GITHUB:
        if base_url or settings.github_base_url:
            return (base_url or settings.github_base_url or "").rstrip("/") + "/models", {
                **bearer,
                **_GITHUB_HEADERS,
            }
        return _GITHUB_CATALOG_URL, {**bearer, **_GITHUB_HEADERS}

    # The OpenAI-compatible providers: ``<base>/models``.
    configured = {
        Provider.OPENAI: settings.openai_base_url or _OPENAI_DEFAULT_BASE,
        Provider.MISTRAL: settings.mistral_base_url or _MISTRAL_BASE,
        Provider.GROQ: settings.groq_base_url or _GROQ_BASE,
        Provider.ZAI: settings.zai_base_url or DEFAULT_BASE_URLS[Provider.ZAI],
        Provider.CUSTOM: settings.custom_base_url,
    }
    if provider in configured:
        base = base_url or configured[provider]
        if not base:
            raise CatalogError("Enter the provider's base URL to list its models.")
        return base.rstrip("/") + "/models", bearer
    raise CatalogError("Listing models is not supported for this provider.")


def _parse(provider: Provider, body: Any) -> list[dict[str, Any]]:
    models: list[dict[str, Any]] = []
    field = "models" if provider == Provider.GOOGLE else "data"
    # GitHub Models answers with a bare array; the rest wrap it in an object.
    items = body if isinstance(body, list) else body.get(field) if isinstance(body, dict) else None
    items = [item for item in items or [] if isinstance(item, dict)]
    if provider == Provider.GOOGLE:
        for item in items:
            if "generateContent" not in (item.get("supportedGenerationMethods") or []):
                continue  # embeddings and the like are not chat models
            model_id = str(item.get("name", "")).removeprefix("models/")
            models.append({"id": model_id, "name": item.get("displayName") or None})
    else:
        for item in items:
            model_id = str(item.get("id", ""))
            entry: dict[str, Any] = {
                "id": model_id,
                "name": item.get("name") or item.get("display_name") or None,
            }
            if provider == Provider.OPENROUTER:
                entry["free"] = model_id.endswith(":free")
            models.append(entry)

    seen: set[str] = set()
    out = []
    for entry in sorted(models, key=lambda e: e["id"]):
        if not entry["id"] or len(entry["id"]) > _MAX_ID or entry["id"] in seen:
            continue
        seen.add(entry["id"])
        out.append(entry)
    return out[:_MAX_MODELS]


async def fetch_models(
    provider: Provider, api_key: str, base_url: str | None = None
) -> list[dict[str, Any]]:
    """The provider's chat-model catalogue as ``[{id, name, free?}]``, sorted by id.

    ``base_url`` lists from that endpoint instead of the provider's default.
    """
    if base_url:
        try:
            base_url = clean_base_url(base_url)
        except ValueError as exc:
            raise CatalogError(str(exc)) from exc
    url, headers = _request(provider, api_key, base_url)
    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
            response = await client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise CatalogError(f"Could not reach {provider.value} to list models.") from exc

    if response.status_code in (401, 403):
        raise CatalogError(f"{provider.value} refused this key when listing models.")
    if response.status_code != 200:
        raise CatalogError(
            f"{provider.value} answered {response.status_code} when listing models."
        )
    try:
        body = response.json()
    except ValueError as exc:
        raise CatalogError(f"{provider.value} returned an unreadable model list.") from exc
    return _parse(provider, body)


async def key_catalog(registry: Any, key_id: str) -> list[dict[str, Any]] | None:
    """The catalogue for an existing pool key, or None if the key is unknown.

    The decrypted key is read and used inside the pool package and never returned,
    so the API layer only ever sees the model list.
    """
    record = await registry.get(key_id)
    if record is None:
        return None
    return await fetch_models(
        record.provider, await registry.get_key_material(key_id), record.base_url
    )
