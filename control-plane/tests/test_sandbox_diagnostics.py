"""API coverage for the account-scoped sandbox diagnostics surface."""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, signup_and_get_api_key


async def _create_session(client: httpx.AsyncClient, api_key: str) -> str:
    response = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert response.status_code == 201, response.text
    return response.json()["id"]


async def test_diagnostics_summary_explains_oom_and_includes_runtime_data(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "diagnostics@example.com")
    session_id = await _create_session(client, key)

    response = await client.get(
        f"/v1/sandboxes/{session_id}/diagnostics/summary",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["why"] == "killed: exceeded memory limit"
    assert body["pod"]["name"].startswith("fake-pod-")
    assert body["logs"][0]["output"] == "process killed"
    assert body["events"][0]["reason"] == "OOMKilled"


async def test_diagnostics_allows_destroyed_sessions(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "diagnostics-destroyed@example.com")
    session_id = await _create_session(client, key)
    response = await client.delete(
        f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"}
    )
    assert response.status_code == 204

    diagnostics = await client.get(
        f"/v1/sandboxes/{session_id}/diagnostics/inspect",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert diagnostics.status_code == 200
    assert diagnostics.json()["status"] == "destroyed"


async def test_diagnostics_preserves_cross_account_404(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    owner_key = await signup_and_get_api_key(client, "diagnostics-owner@example.com")
    other_key = await signup_and_get_api_key(client, "diagnostics-other@example.com")
    session_id = await _create_session(client, owner_key)
    response = await client.get(
        f"/v1/sandboxes/{session_id}/diagnostics/events",
        headers={"Authorization": f"Bearer {other_key}"},
    )
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"
