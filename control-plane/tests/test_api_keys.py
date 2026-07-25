"""API key creation, listing, and revocation."""

from __future__ import annotations

import httpx

from conftest import create_api_key, signup


async def test_create_api_key_returns_raw_key_once(client: httpx.AsyncClient):
    token_response = await signup(client, "keys-user@example.com")
    access_token = token_response["access_token"]

    body = await create_api_key(client, access_token, name="ci key")

    assert body["key"].startswith("bxk_live_")
    assert body["prefix"].startswith("bxk_live_")
    assert body["name"] == "ci key"
    assert body["revoked_at"] is None
    assert body["role"] == "admin"


async def test_create_api_key_defaults_to_admin_role_when_role_omitted(client: httpx.AsyncClient):
    """Backward compatibility: a caller that doesn't know about `role` yet
    (an older SDK, a hand-rolled request) gets today's original behavior
    (full permissions), not a silently restricted key."""
    token_response = await signup(client, "keys-default-role@example.com")
    resp = await client.post(
        "/v1/api-keys",
        json={"name": "no role field"},
        headers={"Authorization": f"Bearer {token_response['access_token']}"},
    )
    assert resp.status_code == 201
    assert resp.json()["role"] == "admin"


async def test_create_api_key_with_member_role(client: httpx.AsyncClient):
    token_response = await signup(client, "keys-member-role@example.com")
    body = await create_api_key(client, token_response["access_token"], name="member key", role="member")

    assert body["role"] == "member"


async def test_create_api_key_rejects_invalid_role(client: httpx.AsyncClient):
    token_response = await signup(client, "keys-invalid-role@example.com")
    resp = await client.post(
        "/v1/api-keys",
        json={"name": "bad role", "role": "superadmin"},
        headers={"Authorization": f"Bearer {token_response['access_token']}"},
    )
    assert resp.status_code == 422


async def test_list_api_keys_includes_role(client: httpx.AsyncClient):
    token_response = await signup(client, "keys-list-role@example.com")
    access_token = token_response["access_token"]
    await create_api_key(client, access_token, name="member key", role="member")

    resp = await client.get("/v1/api-keys", headers={"Authorization": f"Bearer {access_token}"})

    assert resp.status_code == 200
    assert resp.json()[0]["role"] == "member"


async def test_list_api_keys_never_includes_raw_key(client: httpx.AsyncClient):
    token_response = await signup(client, "keys-list@example.com")
    access_token = token_response["access_token"]
    created = await create_api_key(client, access_token, name="listed key")

    resp = await client.get("/v1/api-keys", headers={"Authorization": f"Bearer {access_token}"})

    assert resp.status_code == 200
    keys = resp.json()
    assert len(keys) == 1
    assert keys[0]["id"] == created["id"]
    assert "key" not in keys[0]


async def test_revoked_api_key_can_no_longer_authenticate_sandbox_routes(
    client: httpx.AsyncClient,
):
    token_response = await signup(client, "revoke-user@example.com")
    access_token = token_response["access_token"]
    created = await create_api_key(client, access_token, name="to be revoked")
    raw_key = created["key"]

    # Works before revocation.
    ok = await client.get("/v1/sandboxes", headers={"Authorization": f"Bearer {raw_key}"})
    assert ok.status_code == 200

    revoke_resp = await client.delete(
        f"/v1/api-keys/{created['id']}", headers={"Authorization": f"Bearer {access_token}"}
    )
    assert revoke_resp.status_code == 204

    denied = await client.get("/v1/sandboxes", headers={"Authorization": f"Bearer {raw_key}"})
    assert denied.status_code == 401
    assert denied.json()["error"]["code"] == "invalid_api_key"


async def test_cannot_revoke_another_accounts_api_key(client: httpx.AsyncClient):
    token_a = (await signup(client, "owner-a@example.com"))["access_token"]
    key_a = await create_api_key(client, token_a, name="A's key")

    token_b = (await signup(client, "owner-b@example.com"))["access_token"]

    resp = await client.delete(
        f"/v1/api-keys/{key_a['id']}", headers={"Authorization": f"Bearer {token_b}"}
    )

    assert resp.status_code == 404


async def test_api_key_rejected_on_user_only_routes(client: httpx.AsyncClient):
    """An API key must never work where a dashboard session token is required."""
    token_response = await signup(client, "wrong-cred@example.com")
    created = await create_api_key(client, token_response["access_token"])

    resp = await client.get("/v1/api-keys", headers={"Authorization": f"Bearer {created['key']}"})

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"


async def test_session_token_rejected_on_sandbox_routes(client: httpx.AsyncClient):
    """A dashboard JWT must never work where an API key is required."""
    token_response = await signup(client, "session-not-key@example.com")

    resp = await client.get(
        "/v1/sandboxes", headers={"Authorization": f"Bearer {token_response['access_token']}"}
    )

    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"
