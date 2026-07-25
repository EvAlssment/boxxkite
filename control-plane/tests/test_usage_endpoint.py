"""GET /v1/usage — standalone usage check, same numbers already returned
inline on sandbox creation, but queryable without creating a sandbox.
"""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, signup_and_get_api_key


async def test_usage_reflects_zero_before_any_sandbox(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "usage-fresh@example.com")

    resp = await client.get("/v1/usage", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["concurrent_sandboxes"] == 0
    assert body["monthly_sandbox_hours_used"] == 0.0
    assert "monthly_sandbox_hours_limit" in body
    assert "concurrent_sandboxes_limit" in body


async def test_usage_reflects_active_sandbox(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "usage-active@example.com")
    await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})

    resp = await client.get("/v1/usage", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 200
    assert resp.json()["concurrent_sandboxes"] == 1


async def test_usage_requires_authentication(client: httpx.AsyncClient):
    resp = await client.get("/v1/usage")
    assert resp.status_code == 401


async def test_usage_scoped_per_account(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "usage-a@example.com")
    key_b = await signup_and_get_api_key(client, "usage-b@example.com")
    await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key_a}"})

    resp_b = await client.get("/v1/usage", headers={"Authorization": f"Bearer {key_b}"})
    assert resp_b.json()["concurrent_sandboxes"] == 0
