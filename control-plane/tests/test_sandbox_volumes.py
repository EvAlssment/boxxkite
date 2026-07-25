"""Independent Storage Volumes API --
docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum.

Mirrors test_sandbox_images.py's structure exactly: happy-path create/get/
list/delete first, then limits, then the cross-tenant 404-not-403
guarantees and the "never silently fall back / omit" guarantee on sandbox
create with volume_mounts.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from conftest import FakeSandboxManager, signup_and_get_api_key
from control_plane.config import settings


@pytest.fixture(autouse=True)
def _enable_volumes(monkeypatch):
    # Off by default (opt-in feature) -- every test in this file explicitly
    # turns it on, same rationale as test_sandbox_images.py's own fixture.
    monkeypatch.setattr(settings, "BOXKITE_VOLUMES_ENABLED", True)


async def _create_volume(client: httpx.AsyncClient, key: str, *, label: str = "data-vol", size_gb: float = 10) -> dict:
    resp = await client.post(
        "/v1/volumes",
        json={"label": label, "size_gb": size_gb},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 202, resp.text
    return resp.json()


async def _wait_for_status(client: httpx.AsyncClient, key: str, volume_id: str, *, timeout: float = 2.0) -> dict:
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        resp = await client.get(f"/v1/volumes/{volume_id}", headers={"Authorization": f"Bearer {key}"})
        assert resp.status_code == 200, resp.text
        body = resp.json()
        if body["status"] in {"ready", "failed"}:
            return body
        await asyncio.sleep(0.01)
    raise AssertionError(f"volume {volume_id} never reached a terminal status in time")


async def test_volumes_disabled_by_default(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_VOLUMES_ENABLED", False)
    key = await signup_and_get_api_key(client, "volumes-disabled@example.com")
    resp = await client.post(
        "/v1/volumes", json={"size_gb": 10}, headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 404


async def test_create_volume_is_queued_then_ready(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "volumes-create@example.com")
    accepted = await _create_volume(client, key)
    assert accepted["status"] == "queued"
    assert accepted["label"] == "data-vol"

    final = await _wait_for_status(client, key, accepted["id"])
    assert final["status"] == "ready"
    assert final["pvc_name"] is not None
    assert final["pvc_name"].startswith("boxkite-vol-")


async def test_oversized_volume_fails_provisioning(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "volumes-oversized@example.com")
    accepted = await _create_volume(client, key, size_gb=1000)

    final = await _wait_for_status(client, key, accepted["id"])
    assert final["status"] == "failed"
    assert final["failure_reason"]


async def test_volume_limit_returns_429(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_MAX_VOLUMES_PER_ACCOUNT", 1)
    key = await signup_and_get_api_key(client, "volumes-limit@example.com")
    await _create_volume(client, key, label="one")
    resp = await client.post(
        "/v1/volumes",
        json={"label": "two", "size_gb": 5},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 429
    assert resp.json()["error"]["code"] == "volume_limit_reached"


async def test_account_cannot_get_another_accounts_volume(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "volumes-cross-a@example.com")
    key_b = await signup_and_get_api_key(client, "volumes-cross-b@example.com")
    accepted = await _create_volume(client, key_a)

    resp = await client.get(f"/v1/volumes/{accepted['id']}", headers={"Authorization": f"Bearer {key_b}"})
    assert resp.status_code == 404


async def test_account_cannot_delete_another_accounts_volume(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "volumes-del-a@example.com")
    key_b = await signup_and_get_api_key(client, "volumes-del-b@example.com")
    accepted = await _create_volume(client, key_a)

    resp = await client.delete(f"/v1/volumes/{accepted['id']}", headers={"Authorization": f"Bearer {key_b}"})
    assert resp.status_code == 404


async def test_delete_volume_then_404s(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "volumes-delete@example.com")
    accepted = await _create_volume(client, key)
    await _wait_for_status(client, key, accepted["id"])

    resp = await client.delete(f"/v1/volumes/{accepted['id']}", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 204

    resp = await client.get(f"/v1/volumes/{accepted['id']}", headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 404


async def test_list_volumes_only_returns_own_account(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "volumes-list-a@example.com")
    key_b = await signup_and_get_api_key(client, "volumes-list-b@example.com")
    await _create_volume(client, key_a)

    resp = await client.get("/v1/volumes", headers={"Authorization": f"Bearer {key_b}"})
    assert resp.status_code == 200
    assert resp.json() == []


async def test_create_sandbox_with_ready_volume_passes_pvc_name_to_manager(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "volumes-sandbox-ok@example.com")
    accepted = await _create_volume(client, key)
    final = await _wait_for_status(client, key, accepted["id"])

    resp = await client.post(
        "/v1/sandboxes",
        json={"volume_mounts": {accepted["id"]: "/data"}},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    assert fake_manager.created[session_id]["volume_mounts"] == [
        {"pvc_name": final["pvc_name"], "mount_path": "/data"}
    ]


async def test_create_sandbox_with_queued_volume_404s_never_silently_omits(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch
):
    """Same fail-closed guarantee as image_id: a still-provisioning volume
    must 404 the sandbox create, never silently create the sandbox without
    the volume the caller explicitly asked to mount."""
    key = await signup_and_get_api_key(client, "volumes-sandbox-notready@example.com")

    import control_plane.routers.volumes as volumes_router

    async def _never_finishes(**_kwargs):
        await asyncio.sleep(10)

    monkeypatch.setattr(volumes_router, "dispatch_volume_creation", _never_finishes)

    accepted = await _create_volume(client, key)
    assert accepted["status"] == "queued"

    resp = await client.post(
        "/v1/sandboxes",
        json={"volume_mounts": {accepted["id"]: "/data"}},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    assert not fake_manager.created


async def test_create_sandbox_with_foreign_volume_id_404s(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "volumes-sandbox-foreign-a@example.com")
    key_b = await signup_and_get_api_key(client, "volumes-sandbox-foreign-b@example.com")
    accepted = await _create_volume(client, key_a)
    await _wait_for_status(client, key_a, accepted["id"])

    resp = await client.post(
        "/v1/sandboxes",
        json={"volume_mounts": {accepted["id"]: "/data"}},
        headers={"Authorization": f"Bearer {key_b}"},
    )
    assert resp.status_code == 404


async def test_create_sandbox_without_volume_mounts_behaves_exactly_as_before(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "volumes-sandbox-default@example.com")
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    assert fake_manager.created[session_id]["volume_mounts"] is None
