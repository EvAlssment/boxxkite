"""Sandbox session create/list/delete happy path (same-account), including
the label round-trip and the "already destroyed" / "never existed" 404s.
"""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, signup_and_get_api_key


async def test_create_list_delete_round_trip(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "lifecycle@example.com")

    create_resp = await client.post(
        "/v1/sandboxes", json={"label": "my-experiment"}, headers={"Authorization": f"Bearer {key}"}
    )
    assert create_resp.status_code == 201
    created = create_resp.json()
    assert created["status"] == "active"
    assert created["label"] == "my-experiment"
    assert created["connect"]["pod_name"] == fake_manager.created[created["id"]]["pod_name"]
    assert "expires_at" in created
    assert created["usage"]["concurrent_sandboxes"] == 1

    list_resp = await client.get("/v1/sandboxes", headers={"Authorization": f"Bearer {key}"})
    assert list_resp.status_code == 200
    [listed] = list_resp.json()
    # The label set at creation time must still be there when listed later.
    assert listed["label"] == "my-experiment"
    assert listed["id"] == created["id"]

    delete_resp = await client.delete(
        f"/v1/sandboxes/{created['id']}", headers={"Authorization": f"Bearer {key}"}
    )
    assert delete_resp.status_code == 204
    assert created["id"] in fake_manager.destroyed

    list_after = await client.get("/v1/sandboxes", headers={"Authorization": f"Bearer {key}"})
    [after] = list_after.json()
    assert after["status"] == "destroyed"
    assert after["connect"] is None


async def test_delete_unknown_session_id_returns_404(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "unknown-session@example.com")

    resp = await client.delete(
        "/v1/sandboxes/00000000-0000-0000-0000-000000000000",
        headers={"Authorization": f"Bearer {key}"},
    )

    assert resp.status_code == 404


async def test_delete_already_destroyed_session_returns_404(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "double-delete@example.com")
    create_resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    session_id = create_resp.json()["id"]

    first = await client.delete(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"})
    assert first.status_code == 204

    second = await client.delete(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"})
    assert second.status_code == 404
    # SandboxManager.destroy_session must not be called twice for one session.
    assert fake_manager.destroyed.count(session_id) == 1


async def test_sandbox_routes_require_authentication(client: httpx.AsyncClient):
    resp = await client.get("/v1/sandboxes")
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_credentials"
