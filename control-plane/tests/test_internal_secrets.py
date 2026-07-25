"""Tests for POST /internal/secrets/resolve -- the sidecar-only,
capability-token-authenticated endpoint that resolves a granted secret's
plaintext value (docs/SECRETS-DESIGN.md §4).

Covers:
- A valid, correctly-scoped token resolves the real value.
- A token bound to a different session_id is rejected.
- A secret name not in the token's own grant list 404s identically to a
  name that doesn't exist at all (never distinguishable -- §3).
- A dashboard JWT or a raw API key is never accepted here (this endpoint
  has its own, third, credential type).
"""

from __future__ import annotations

import httpx

from conftest import create_api_key, signup
from control_plane.secret_capability import create_capability_token


async def _account_with_key(client: httpx.AsyncClient, email: str) -> str:
    token_response = await signup(client, email)
    created = await create_api_key(client, token_response["access_token"], name="ci key")
    return created["key"], token_response["access_token"]


async def _create_secret(client: httpx.AsyncClient, api_key: str, name: str, value: str, hosts: list[str]) -> str:
    resp = await client.post(
        "/v1/secrets",
        json={"name": name, "value": value, "allowed_hosts": hosts},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def test_resolve_returns_plaintext_for_a_validly_granted_secret(client: httpx.AsyncClient):
    api_key, _jwt = await _account_with_key(client, "internal-resolve@example.com")
    me = await client.get("/v1/account", headers={"Authorization": f"Bearer {api_key}"})
    assert me.status_code == 200
    account_id = me.json()["id"]
    assert account_id

    await _create_secret(client, api_key, "granted", "the-real-plaintext-value", ["example.com"])

    token = create_capability_token(
        account_id=account_id, session_id="session-xyz", secret_names=["granted"]
    )

    resp = await client.post(
        "/internal/secrets/resolve",
        json={"session_id": "session-xyz", "secret_name": "granted"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"name": "granted", "value": "the-real-plaintext-value"}


async def test_resolve_rejects_token_bound_to_a_different_session(client: httpx.AsyncClient):
    api_key, _jwt = await _account_with_key(client, "internal-wrong-session@example.com")
    me = await client.get("/v1/account", headers={"Authorization": f"Bearer {api_key}"})
    account_id = me.json()["id"]

    await _create_secret(client, api_key, "granted", "value", ["example.com"])
    token = create_capability_token(
        account_id=account_id, session_id="session-A", secret_names=["granted"]
    )

    resp = await client.post(
        "/internal/secrets/resolve",
        json={"session_id": "session-B", "secret_name": "granted"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_capability_token"


async def test_resolve_404s_identically_for_ungranted_and_nonexistent_names(client: httpx.AsyncClient):
    api_key, _jwt = await _account_with_key(client, "internal-not-granted@example.com")
    me = await client.get("/v1/account", headers={"Authorization": f"Bearer {api_key}"})
    account_id = me.json()["id"]

    await _create_secret(client, api_key, "granted", "value", ["example.com"])
    # Token only grants "granted", not "ungranted".
    token = create_capability_token(
        account_id=account_id, session_id="session-xyz", secret_names=["granted"]
    )

    ungranted_resp = await client.post(
        "/internal/secrets/resolve",
        json={"session_id": "session-xyz", "secret_name": "ungranted"},
        headers={"Authorization": f"Bearer {token}"},
    )
    nonexistent_resp = await client.post(
        "/internal/secrets/resolve",
        json={"session_id": "session-xyz", "secret_name": "totally-made-up"},
        headers={"Authorization": f"Bearer {create_capability_token(account_id=account_id, session_id='session-xyz', secret_names=['totally-made-up'])}"},
    )

    assert ungranted_resp.status_code == 404
    assert ungranted_resp.json()["error"]["code"] == "secret_not_referenced_by_session"
    assert nonexistent_resp.status_code == 404
    assert nonexistent_resp.json()["error"]["code"] == "secret_not_referenced_by_session"


async def test_resolve_rejects_a_dashboard_jwt(client: httpx.AsyncClient):
    token_response = await signup(client, "internal-jwt-rejected@example.com")
    jwt = token_response["access_token"]

    resp = await client.post(
        "/internal/secrets/resolve",
        json={"session_id": "session-xyz", "secret_name": "granted"},
        headers={"Authorization": f"Bearer {jwt}"},
    )
    assert resp.status_code == 401


async def test_resolve_rejects_a_raw_api_key(client: httpx.AsyncClient):
    api_key, _jwt = await _account_with_key(client, "internal-apikey-rejected@example.com")

    resp = await client.post(
        "/internal/secrets/resolve",
        json={"session_id": "session-xyz", "secret_name": "granted"},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 401
