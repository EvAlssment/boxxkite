"""Global concurrent-sandbox cap -- cluster-wide across ALL accounts,
independent of (and checked in addition to) the per-account cap. Two
accounts each well under their own per-account limit can still
collectively hit the global ceiling.
"""

from __future__ import annotations

import httpx

from conftest import signup_and_get_api_key
from control_plane.config import settings


async def test_global_cap_blocks_a_second_account_even_under_its_own_per_account_limit(
    client: httpx.AsyncClient, monkeypatch
):
    monkeypatch.setattr(settings, "BOXKITE_MAX_CONCURRENT_SANDBOXES", 5)
    monkeypatch.setattr(settings, "BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES", 1)

    key_a = await signup_and_get_api_key(client, "global-cap-a@example.com")
    key_b = await signup_and_get_api_key(client, "global-cap-b@example.com")

    first = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key_a}"})
    assert first.status_code == 201

    second = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key_b}"})
    assert second.status_code == 429
    body = second.json()
    assert body["error"]["code"] == "global_capacity_reached"
    assert "$" not in body["error"]["message"]


async def test_destroying_any_accounts_session_frees_a_global_slot(
    client: httpx.AsyncClient, monkeypatch
):
    monkeypatch.setattr(settings, "BOXKITE_MAX_CONCURRENT_SANDBOXES", 5)
    monkeypatch.setattr(settings, "BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES", 1)

    key_a = await signup_and_get_api_key(client, "global-cap-free-a@example.com")
    key_b = await signup_and_get_api_key(client, "global-cap-free-b@example.com")

    first = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key_a}"})
    session_id = first.json()["id"]

    blocked = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key_b}"})
    assert blocked.status_code == 429

    await client.delete(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key_a}"})

    second = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key_b}"})
    assert second.status_code == 201


async def test_global_cap_checked_before_pod_creation(client: httpx.AsyncClient, monkeypatch, fake_manager):
    """The global check must happen before SandboxManager.create_session is
    called at all -- no pod should be created for a request that's about to
    be rejected on capacity grounds."""
    monkeypatch.setattr(settings, "BOXKITE_GLOBAL_MAX_CONCURRENT_SANDBOXES", 0)
    key = await signup_and_get_api_key(client, "global-cap-no-pod@example.com")

    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})

    assert resp.status_code == 429
    assert fake_manager.created == {}
