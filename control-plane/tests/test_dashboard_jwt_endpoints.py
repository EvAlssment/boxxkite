"""JWT-authenticated read-only mirrors of the API-key routes, added so a
browser dashboard can authenticate via login (JWT) instead of asking a user
to paste a long-lived API key into a browser session.

These are strictly additive: `/v1/account` (API key), `/v1/sandboxes` (API
key), and `/v1/usage` (API key) are untouched -- see test_account_endpoint.py,
test_sandbox_lifecycle.py, and test_usage_endpoint.py, all still passing.
This file covers the three new routes:

- GET /v1/account/me       (JWT) -- identity, mirrors GET /v1/account
- GET /v1/account/sandboxes (JWT) -- list, mirrors GET /v1/sandboxes
- GET /v1/account/usage    (JWT) -- usage, mirrors GET /v1/usage
"""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, create_api_key, signup


async def test_account_me_returns_identity_for_dashboard_jwt(client: httpx.AsyncClient):
    signup_resp = await signup(client, "dash-me@example.com")

    resp = await client.get(
        "/v1/account/me", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "dash-me@example.com"
    assert "id" in body
    assert "created_at" in body


async def test_account_me_rejects_api_key(client: httpx.AsyncClient):
    signup_resp = await signup(client, "dash-me-wrong-cred@example.com")
    created = await create_api_key(client, signup_resp["access_token"])

    resp = await client.get("/v1/account/me", headers={"Authorization": f"Bearer {created['key']}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"


async def test_account_me_requires_authentication(client: httpx.AsyncClient):
    resp = await client.get("/v1/account/me")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_credentials"


async def test_account_sandboxes_lists_sessions_for_dashboard_jwt(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    signup_resp = await signup(client, "dash-sandboxes@example.com")
    access_token = signup_resp["access_token"]
    created = await create_api_key(client, access_token)

    create_resp = await client.post(
        "/v1/sandboxes", json={"label": "via api key"}, headers={"Authorization": f"Bearer {created['key']}"}
    )
    assert create_resp.status_code == 201

    resp = await client.get("/v1/account/sandboxes", headers={"Authorization": f"Bearer {access_token}"})
    assert resp.status_code == 200
    body = resp.json()
    assert len(body) == 1
    assert body[0]["label"] == "via api key"
    assert body[0]["status"] == "active"


async def test_account_sandboxes_active_only_query_param(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    signup_resp = await signup(client, "dash-sandboxes-active@example.com")
    access_token = signup_resp["access_token"]
    created = await create_api_key(client, access_token)
    api_key_header = {"Authorization": f"Bearer {created['key']}"}

    create_resp = await client.post("/v1/sandboxes", json={}, headers=api_key_header)
    session_id = create_resp.json()["id"]
    await client.delete(f"/v1/sandboxes/{session_id}", headers=api_key_header)

    jwt_header = {"Authorization": f"Bearer {access_token}"}
    resp_all = await client.get("/v1/account/sandboxes", headers=jwt_header)
    assert len(resp_all.json()) == 1

    resp_active = await client.get("/v1/account/sandboxes?active_only=true", headers=jwt_header)
    assert resp_active.json() == []


async def test_account_sandboxes_rejects_api_key(client: httpx.AsyncClient):
    signup_resp = await signup(client, "dash-sandboxes-wrong-cred@example.com")
    created = await create_api_key(client, signup_resp["access_token"])

    resp = await client.get("/v1/account/sandboxes", headers={"Authorization": f"Bearer {created['key']}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"


async def test_account_sandboxes_scoped_per_account(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    signup_a = await signup(client, "dash-sandboxes-a@example.com")
    key_a = await create_api_key(client, signup_a["access_token"])
    await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key_a['key']}"})

    signup_b = await signup(client, "dash-sandboxes-b@example.com")

    resp_b = await client.get(
        "/v1/account/sandboxes", headers={"Authorization": f"Bearer {signup_b['access_token']}"}
    )
    assert resp_b.status_code == 200
    assert resp_b.json() == []


async def test_account_usage_reflects_zero_before_any_sandbox(client: httpx.AsyncClient):
    signup_resp = await signup(client, "dash-usage-fresh@example.com")

    resp = await client.get(
        "/v1/account/usage", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["concurrent_sandboxes"] == 0
    assert body["monthly_sandbox_hours_used"] == 0.0
    assert "monthly_sandbox_hours_limit" in body
    assert "concurrent_sandboxes_limit" in body


async def test_account_usage_reflects_active_sandbox(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    signup_resp = await signup(client, "dash-usage-active@example.com")
    access_token = signup_resp["access_token"]
    created = await create_api_key(client, access_token)
    await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {created['key']}"})

    resp = await client.get("/v1/account/usage", headers={"Authorization": f"Bearer {access_token}"})
    assert resp.status_code == 200
    assert resp.json()["concurrent_sandboxes"] == 1


async def test_account_usage_rejects_api_key(client: httpx.AsyncClient):
    signup_resp = await signup(client, "dash-usage-wrong-cred@example.com")
    created = await create_api_key(client, signup_resp["access_token"])

    resp = await client.get("/v1/account/usage", headers={"Authorization": f"Bearer {created['key']}"})
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"


async def test_account_usage_requires_authentication(client: httpx.AsyncClient):
    resp = await client.get("/v1/account/usage")
    assert resp.status_code == 401


async def test_existing_api_key_account_route_unaffected(client: httpx.AsyncClient):
    """Guard against accidentally widening /v1/account's own auth
    requirement while adding the new JWT-mirrored routes alongside it."""
    signup_resp = await signup(client, "dash-guard-account@example.com")

    resp = await client.get(
        "/v1/account", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"


async def test_existing_api_key_sandboxes_route_unaffected(client: httpx.AsyncClient):
    signup_resp = await signup(client, "dash-guard-sandboxes@example.com")

    resp = await client.get(
        "/v1/sandboxes", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"


async def test_existing_api_key_usage_route_unaffected(client: httpx.AsyncClient):
    signup_resp = await signup(client, "dash-guard-usage@example.com")

    resp = await client.get(
        "/v1/usage", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"
