"""GET /v1/account -- account identity via API key, for `boxkite whoami`.
API keys are already scoped to one account for every other /v1/sandboxes
and /v1/usage call; this is the same trust boundary, not a new one.
"""

from __future__ import annotations

import httpx

from conftest import create_api_key, signup


async def test_account_returns_email_for_the_authenticated_key(client: httpx.AsyncClient):
    signup_resp = await signup(client, "whoami@example.com")
    created = await create_api_key(client, signup_resp["access_token"], name="whoami key")

    resp = await client.get("/v1/account", headers={"Authorization": f"Bearer {created['key']}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["email"] == "whoami@example.com"
    assert "id" in body


async def test_account_requires_api_key_not_dashboard_token(client: httpx.AsyncClient):
    signup_resp = await signup(client, "whoami-wrong-cred@example.com")

    resp = await client.get(
        "/v1/account", headers={"Authorization": f"Bearer {signup_resp['access_token']}"}
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "wrong_credential_type"
