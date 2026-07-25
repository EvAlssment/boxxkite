"""The single most important test in this suite: account A must not be able
to observe, list, or destroy account B's sandbox sessions through any
combination of inputs — not just "the happy path doesn't leak them".
"""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, signup_and_get_api_key


async def test_account_cannot_list_another_accounts_sandbox_sessions(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "tenant-a@example.com")
    key_b = await signup_and_get_api_key(client, "tenant-b@example.com")

    create_resp = await client.post(
        "/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key_a}"}
    )
    assert create_resp.status_code == 201
    session_a_id = create_resp.json()["id"]

    # B's own list must be empty.
    list_as_b = await client.get("/v1/sandboxes", headers={"Authorization": f"Bearer {key_b}"})
    assert list_as_b.status_code == 200
    assert list_as_b.json() == []

    # A's list must show exactly the one session A created.
    list_as_a = await client.get("/v1/sandboxes", headers={"Authorization": f"Bearer {key_a}"})
    assert list_as_a.status_code == 200
    ids = [s["id"] for s in list_as_a.json()]
    assert ids == [session_a_id]


async def test_account_cannot_delete_another_accounts_sandbox_session(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "victim@example.com")
    key_b = await signup_and_get_api_key(client, "attacker@example.com")

    create_resp = await client.post(
        "/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key_a}"}
    )
    session_a_id = create_resp.json()["id"]

    # B attempts to destroy A's session by guessing/observing its id.
    delete_as_b = await client.delete(
        f"/v1/sandboxes/{session_a_id}", headers={"Authorization": f"Bearer {key_b}"}
    )

    # 404, not 403 — B must not be able to distinguish "not yours" from
    # "doesn't exist" for a session_id it has no business knowing about.
    assert delete_as_b.status_code == 404
    assert delete_as_b.json()["error"]["code"] == "not_found"

    # The underlying SandboxManager must never have been asked to tear it down.
    assert session_a_id not in fake_manager.destroyed

    # A can still see and destroy their own session afterwards.
    list_as_a = await client.get("/v1/sandboxes", headers={"Authorization": f"Bearer {key_a}"})
    assert [s["id"] for s in list_as_a.json()] == [session_a_id]

    delete_as_a = await client.delete(
        f"/v1/sandboxes/{session_a_id}", headers={"Authorization": f"Bearer {key_a}"}
    )
    assert delete_as_a.status_code == 204
    assert session_a_id in fake_manager.destroyed


async def test_account_cannot_probe_existence_of_foreign_session_via_active_only_filter(
    client: httpx.AsyncClient,
):
    key_a = await signup_and_get_api_key(client, "filter-victim@example.com")
    key_b = await signup_and_get_api_key(client, "filter-attacker@example.com")

    await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key_a}"})

    resp = await client.get(
        "/v1/sandboxes?active_only=true", headers={"Authorization": f"Bearer {key_b}"}
    )

    assert resp.status_code == 200
    assert resp.json() == []


async def test_organization_id_passed_to_sandbox_manager_is_the_owning_account(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    """SandboxManager's own storage isolation is keyed by organization_id —
    assert the control plane actually passes the *creating* account's id,
    not e.g. a shared/default value, since that's the mechanism that keeps
    two tenants' sandbox storage prefixes apart at the SandboxManager layer."""
    key_a = await signup_and_get_api_key(client, "org-scope@example.com")

    create_resp = await client.post(
        "/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key_a}"}
    )
    session_id = create_resp.json()["id"]

    assert fake_manager.created[session_id]["organization_id"] is not None
    # Two different accounts must never produce the same organization_id.
    key_b = await signup_and_get_api_key(client, "org-scope-2@example.com")
    create_resp_b = await client.post(
        "/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key_b}"}
    )
    session_id_b = create_resp_b.json()["id"]
    assert (
        fake_manager.created[session_id]["organization_id"]
        != fake_manager.created[session_id_b]["organization_id"]
    )
