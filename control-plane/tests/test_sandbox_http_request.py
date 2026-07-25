"""Tests for POST /v1/sandboxes/{id}/http-request -- the control-plane
proxy to SandboxManager.http_request() (docs/SECRETS-DESIGN.md).
"""

from __future__ import annotations

import httpx

from conftest import create_api_key, signup


async def _account_with_key(client: httpx.AsyncClient, email: str) -> str:
    token_response = await signup(client, email)
    created = await create_api_key(client, token_response["access_token"], name="ci key")
    return created["key"]


async def test_http_request_proxies_to_manager(client: httpx.AsyncClient, fake_manager):
    api_key = await _account_with_key(client, "http-request-proxy@example.com")
    created = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    session_id = created.json()["id"]

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/http-request",
        json={
            "method": "post",
            "url": "https://api.example.com/v1/charges",
            "headers": {"Authorization": "Bearer {{secret:prod-stripe}}"},
            "body": "amount=2000",
        },
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == {
        "status_code": 200,
        "headers": {"content-type": "text/plain"},
        "body": "ok",
        "truncated": False,
    }
    assert fake_manager.http_request_calls == [
        {
            "session_id": session_id,
            "method": "post",
            "url": "https://api.example.com/v1/charges",
            "headers": {"Authorization": "Bearer {{secret:prod-stripe}}"},
            "body": "amount=2000",
            "timeout": 15,
        }
    ]


async def test_http_request_404s_for_a_foreign_session(client: httpx.AsyncClient, fake_manager):
    api_key_a = await _account_with_key(client, "http-request-owner-a@example.com")
    api_key_b = await _account_with_key(client, "http-request-owner-b@example.com")

    created = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key_a}"})
    session_id = created.json()["id"]

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/http-request",
        json={"method": "GET", "url": "https://api.example.com/"},
        headers={"Authorization": f"Bearer {api_key_b}"},
    )
    assert resp.status_code == 404
    assert fake_manager.http_request_calls == []
