"""Account creation and API key lifecycle."""

import pytest
from httpx import AsyncClient


@pytest.mark.asyncio
async def test_create_account_returns_first_key(async_client: AsyncClient):
    """Create account issues a key usable on first request."""
    resp = await async_client.post(
        "/v1/accounts",
        json={"display_name": "Test User"},
    )
    assert resp.status_code == 201
    data = resp.json()

    assert data["object"] == "account"
    assert data["id"]
    assert data["display_name"] == "Test User"
    assert data["created_at"]
    assert len(data["keys"]) == 1

    key = data["keys"][0]
    assert key["key"].startswith("pgw_live_")
    assert key["prefix"]
    assert key["id"]

    account_id = data["id"]
    raw_key = key["key"]

    # Test: raw key authenticates immediately
    resp2 = await async_client.get(
        f"/v1/accounts/{account_id}/keys",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp2.status_code == 200


@pytest.mark.asyncio
async def test_missing_authorization_header_returns_401(async_client: AsyncClient):
    """Missing API key → 401 in OpenAI error shape."""
    resp = await async_client.get(
        "/v1/accounts/fake-id/keys",
    )
    assert resp.status_code == 401
    data = resp.json()

    assert "error" in data
    assert data["error"]["type"] == "authentication_error"
    assert data["error"]["code"] == "missing_api_key"


@pytest.mark.asyncio
async def test_invalid_key_returns_401(async_client: AsyncClient):
    """Invalid/revoked key → 401 in OpenAI error shape."""
    resp = await async_client.get(
        "/v1/accounts/fake-id/keys",
        headers={"Authorization": "Bearer invalid_key"},
    )
    assert resp.status_code == 401
    data = resp.json()

    assert data["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_list_keys_returns_prefix_only(async_client: AsyncClient):
    """List returns prefix only, never raw key or hash."""
    # Create account
    resp = await async_client.post(
        "/v1/accounts",
        json={},
    )
    account_id = resp.json()["id"]
    raw_key = resp.json()["keys"][0]["key"]

    # Create additional keys
    resp2 = await async_client.post(
        f"/v1/accounts/{account_id}/keys",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp2.status_code == 201

    # List keys
    resp3 = await async_client.get(
        f"/v1/accounts/{account_id}/keys",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp3.status_code == 200
    keys = resp3.json()

    assert len(keys) >= 2
    for key in keys:
        assert "id" in key
        assert "prefix" in key
        assert "created_at" in key
        assert "status" in key
        assert "key" not in key
        assert "key_hash" not in key


@pytest.mark.asyncio
async def test_revoke_key_immediate_effect(async_client: AsyncClient):
    """Revocation takes effect immediately."""
    # Create account
    resp = await async_client.post(
        "/v1/accounts",
        json={},
    )
    data = resp.json()
    account_id = data["id"]
    key_id = data["keys"][0]["id"]
    raw_key = data["keys"][0]["key"]

    # Revoke the key
    resp2 = await async_client.delete(
        f"/v1/accounts/{account_id}/keys/{key_id}",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp2.status_code == 204

    # Next request on revoked key → 401
    resp3 = await async_client.get(
        f"/v1/accounts/{account_id}/keys",
        headers={"Authorization": f"Bearer {raw_key}"},
    )
    assert resp3.status_code == 401


@pytest.mark.asyncio
async def test_cross_account_access_denied(async_client: AsyncClient):
    """Cross-account access → 403 ownership guard."""
    # Create two accounts
    resp1 = await async_client.post("/v1/accounts", json={})
    key1 = resp1.json()["keys"][0]["key"]

    resp2 = await async_client.post("/v1/accounts", json={})
    account2_id = resp2.json()["id"]

    # Try to list account2's keys using account1's key
    resp3 = await async_client.get(
        f"/v1/accounts/{account2_id}/keys",
        headers={"Authorization": f"Bearer {key1}"},
    )
    assert resp3.status_code == 403
    assert resp3.json()["error"]["code"] == "forbidden"


@pytest.mark.asyncio
async def test_revoke_another_accounts_key_denied(async_client: AsyncClient):
    """IDOR guard: account A cannot revoke account B's key via A's own path."""
    resp_a = await async_client.post("/v1/accounts", json={})
    account_a_id = resp_a.json()["id"]
    key_a = resp_a.json()["keys"][0]["key"]

    resp_b = await async_client.post("/v1/accounts", json={})
    key_b_id = resp_b.json()["keys"][0]["id"]
    key_b_raw = resp_b.json()["keys"][0]["key"]

    # A calls DELETE on its own path, targeting B's key_id.
    resp = await async_client.delete(
        f"/v1/accounts/{account_a_id}/keys/{key_b_id}",
        headers={"Authorization": f"Bearer {key_a}"},
    )
    assert resp.status_code == 204

    # B's key must remain usable — the revoke must not have applied.
    resp_check = await async_client.get(
        f"/v1/accounts/{resp_b.json()['id']}/keys",
        headers={"Authorization": f"Bearer {key_b_raw}"},
    )
    assert resp_check.status_code == 200


@pytest.mark.asyncio
async def test_raw_key_not_in_logs(async_client: AsyncClient, caplog):
    """Raw key never appears in logs."""
    resp = await async_client.post(
        "/v1/accounts",
        json={"display_name": "Test"},
    )
    assert resp.status_code == 201
    raw_key = resp.json()["keys"][0]["key"]

    # Verify raw key not in logs
    assert raw_key not in caplog.text


@pytest.mark.asyncio
async def test_create_additional_key(async_client: AsyncClient):
    """Additional key creation works, raw key shown once."""
    # Create account
    resp1 = await async_client.post("/v1/accounts", json={})
    account_id = resp1.json()["id"]
    key1 = resp1.json()["keys"][0]["key"]

    # Create additional key
    resp2 = await async_client.post(
        f"/v1/accounts/{account_id}/keys",
        headers={"Authorization": f"Bearer {key1}"},
    )
    assert resp2.status_code == 201
    data = resp2.json()

    assert data["object"] == "api_key"
    assert data["key"].startswith("pgw_live_")
    assert "key" in data

    new_key = data["key"]

    # Verify new key works
    resp3 = await async_client.get(
        f"/v1/accounts/{account_id}/keys",
        headers={"Authorization": f"Bearer {new_key}"},
    )
    assert resp3.status_code == 200


@pytest.mark.asyncio
async def test_invalid_authorization_header(async_client: AsyncClient):
    """Invalid Bearer format → 401."""
    resp = await async_client.get(
        "/v1/accounts/fake-id/keys",
        headers={"Authorization": "NotBearer token"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_api_key"


@pytest.mark.asyncio
async def test_healthz_endpoint(async_client: AsyncClient):
    """GET /healthz returns 200."""
    resp = await async_client.get("/healthz")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"
