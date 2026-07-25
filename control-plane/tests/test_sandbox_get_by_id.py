"""GET /v1/sandboxes/{id} — single-session lookup, same ownership rules as
list/delete: a foreign or nonexistent session_id 404s identically.
"""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, signup_and_get_api_key


async def test_get_by_id_returns_active_session(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "get-by-id@example.com")

    create_resp = await client.post(
        "/v1/sandboxes", json={"label": "lookup-me"}, headers={"Authorization": f"Bearer {key}"}
    )
    session_id = create_resp.json()["id"]

    get_resp = await client.get(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"})
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["id"] == session_id
    assert body["status"] == "active"
    assert body["label"] == "lookup-me"
    assert body["connect"]["pod_name"] == fake_manager.created[session_id]["pod_name"]


async def test_get_by_id_returns_destroyed_session_not_404(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """Unlike the exec/file routes, GET by id should still resolve a
    destroyed session — this is the "what happened to this one" lookup, not
    an operational route that requires a live pod."""
    key = await signup_and_get_api_key(client, "get-destroyed@example.com")
    create_resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    session_id = create_resp.json()["id"]

    await client.delete(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"})

    get_resp = await client.get(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"})
    assert get_resp.status_code == 200
    body = get_resp.json()
    assert body["status"] == "destroyed"
    assert body["connect"] is None


async def test_get_by_id_unknown_session_returns_404(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "get-unknown@example.com")

    resp = await client.get(
        "/v1/sandboxes/00000000-0000-0000-0000-000000000000",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


async def test_get_by_id_cross_tenant_returns_404(client: httpx.AsyncClient):
    """A session_id belonging to a different account must 404 identically
    to "never existed" -- same cross-tenant guarantee as list/delete."""
    key_a = await signup_and_get_api_key(client, "tenant-a@example.com")
    key_b = await signup_and_get_api_key(client, "tenant-b@example.com")

    create_resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key_a}"})
    session_id = create_resp.json()["id"]

    resp = await client.get(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key_b}"})
    assert resp.status_code == 404
