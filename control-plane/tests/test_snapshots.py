"""Filesystem snapshot/restore -- docs/SNAPSHOT-DESIGN.md.

Mirrors test_sandbox_cross_tenant.py/test_sandbox_get_by_id.py's structure:
happy-path create/list/get/restore/delete first, then the cross-tenant
404-not-403 guarantees, which are the highest-severity risk called out in
the design doc's security section.
"""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, FakeSnapshotStorageClient, signup_and_get_api_key
from test_usage_limits import _assert_no_pricing_language


async def _create_session_with_file(client: httpx.AsyncClient, key: str, *, path: str = "hello.txt") -> str:
    create_resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    assert create_resp.status_code == 201, create_resp.text
    session_id = create_resp.json()["id"]
    file_resp = await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": path, "content": "hello snapshot"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert file_resp.status_code == 200, file_resp.text
    return session_id


async def test_create_snapshot_returns_completed_with_size(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager, fake_snapshot_storage: FakeSnapshotStorageClient
):
    key = await signup_and_get_api_key(client, "snap-create@example.com")
    session_id = await _create_session_with_file(client, key)

    # Seed the fake manager's reported source prefix with a known size so
    # we can assert size_bytes round-trips through the storage-side copy.
    source_prefix = f"sessions/fake-org/{session_id}"
    fake_snapshot_storage.seed(source_prefix, {"workspace/hello.txt": 15})

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/snapshots",
        json={"label": "before-refactor"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "completed"
    assert body["session_id"] == session_id
    assert body["label"] == "before-refactor"
    assert body["size_bytes"] == 15
    assert body["storage_key_prefix"].startswith("snapshots/")
    assert len(fake_snapshot_storage.copy_calls) == 1
    assert fake_snapshot_storage.copy_calls[0]["keys"] == ["workspace/hello.txt"]


async def test_create_snapshot_requires_live_session(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "snap-dead-session@example.com")
    session_id = await _create_session_with_file(client, key)
    await client.delete(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"})

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/snapshots", json={}, headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_snapshot_quota_returns_429_snapshot_limit_reached(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    from control_plane.config import settings

    key = await signup_and_get_api_key(client, "snap-quota@example.com")
    session_id = await _create_session_with_file(client, key)

    original_limit = settings.BOXKITE_MAX_SNAPSHOTS_PER_ACCOUNT
    settings.BOXKITE_MAX_SNAPSHOTS_PER_ACCOUNT = 1
    try:
        first = await client.post(
            f"/v1/sandboxes/{session_id}/snapshots", json={}, headers={"Authorization": f"Bearer {key}"}
        )
        assert first.status_code == 201, first.text

        second = await client.post(
            f"/v1/sandboxes/{session_id}/snapshots", json={}, headers={"Authorization": f"Bearer {key}"}
        )
        assert second.status_code == 429
        assert second.json()["error"]["code"] == "snapshot_limit_reached"
    finally:
        settings.BOXKITE_MAX_SNAPSHOTS_PER_ACCOUNT = original_limit


async def test_create_snapshot_storage_failure_marks_snapshot_failed(
    client: httpx.AsyncClient, fake_snapshot_storage: FakeSnapshotStorageClient
):
    key = await signup_and_get_api_key(client, "snap-storage-fail@example.com")
    session_id = await _create_session_with_file(client, key)

    fake_snapshot_storage.fail_next_copy = True
    resp = await client.post(
        f"/v1/sandboxes/{session_id}/snapshots", json={}, headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 502
    assert resp.json()["error"]["code"] == "snapshot_storage_failed"

    # The failed snapshot must not silently linger as "pending" forever --
    # and it should still be visible (as failed), not disappear.
    list_resp = await client.get(
        f"/v1/sandboxes/{session_id}/snapshots", headers={"Authorization": f"Bearer {key}"}
    )
    assert list_resp.status_code == 200
    statuses = [s["status"] for s in list_resp.json()]
    assert statuses == ["failed"]


async def test_list_snapshots_for_destroyed_session_still_works(client: httpx.AsyncClient):
    """A snapshot outlives its source session -- listing must not require
    the session to still be live."""
    key = await signup_and_get_api_key(client, "snap-list-destroyed@example.com")
    session_id = await _create_session_with_file(client, key)

    create_resp = await client.post(
        f"/v1/sandboxes/{session_id}/snapshots", json={}, headers={"Authorization": f"Bearer {key}"}
    )
    assert create_resp.status_code == 201
    snapshot_id = create_resp.json()["id"]

    await client.delete(f"/v1/sandboxes/{session_id}", headers={"Authorization": f"Bearer {key}"})

    list_resp = await client.get(
        f"/v1/sandboxes/{session_id}/snapshots", headers={"Authorization": f"Bearer {key}"}
    )
    assert list_resp.status_code == 200
    assert [s["id"] for s in list_resp.json()] == [snapshot_id]


async def test_get_snapshot_by_id(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "snap-get@example.com")
    session_id = await _create_session_with_file(client, key)
    create_resp = await client.post(
        f"/v1/sandboxes/{session_id}/snapshots", json={"label": "x"}, headers={"Authorization": f"Bearer {key}"}
    )
    snapshot_id = create_resp.json()["id"]

    get_resp = await client.get(f"/v1/snapshots/{snapshot_id}", headers={"Authorization": f"Bearer {key}"})
    assert get_resp.status_code == 200
    assert get_resp.json()["id"] == snapshot_id
    assert get_resp.json()["label"] == "x"


async def test_get_snapshot_unknown_id_returns_404(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "snap-get-unknown@example.com")
    resp = await client.get(
        "/v1/snapshots/00000000-0000-0000-0000-000000000000",
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


async def test_restore_snapshot_creates_new_session_seeded_from_snapshot(
    client: httpx.AsyncClient,
    fake_manager: FakeSandboxManager,
    fake_snapshot_storage: FakeSnapshotStorageClient,
):
    key = await signup_and_get_api_key(client, "snap-restore@example.com")
    session_id = await _create_session_with_file(client, key)

    create_resp = await client.post(
        f"/v1/sandboxes/{session_id}/snapshots", json={}, headers={"Authorization": f"Bearer {key}"}
    )
    snapshot_id = create_resp.json()["id"]
    storage_key_prefix = create_resp.json()["storage_key_prefix"]

    restore_resp = await client.post(
        f"/v1/snapshots/{snapshot_id}/restore",
        json={"label": "restored-session"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert restore_resp.status_code == 201, restore_resp.text
    body = restore_resp.json()
    assert body["label"] == "restored-session"
    assert body["status"] == "active"
    new_session_id = body["id"]
    assert new_session_id != session_id

    # The new session's storage prefix must have been seeded from the
    # snapshot's own immutable prefix -- never the other way around.
    seed_calls = [c for c in fake_snapshot_storage.copy_calls if c["source_prefix"] == storage_key_prefix]
    assert len(seed_calls) == 1
    assert seed_calls[0]["dest_prefix"].startswith("sessions/")
    assert seed_calls[0]["dest_prefix"].endswith(f"/{new_session_id}")

    # Restored session actually exists via SandboxManager, just like a
    # normal create -- no special-cased pod-spec/capability path.
    assert new_session_id in fake_manager.created
    assert fake_manager.created[new_session_id]["restore_from_snapshot_id"] == snapshot_id


async def test_restore_unknown_snapshot_returns_404(client: httpx.AsyncClient):
    key = await signup_and_get_api_key(client, "snap-restore-unknown@example.com")
    resp = await client.post(
        "/v1/snapshots/00000000-0000-0000-0000-000000000000/restore",
        json={},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 404


async def test_delete_snapshot_removes_storage_objects_and_404s_after(
    client: httpx.AsyncClient, fake_snapshot_storage: FakeSnapshotStorageClient
):
    key = await signup_and_get_api_key(client, "snap-delete@example.com")
    session_id = await _create_session_with_file(client, key)
    create_resp = await client.post(
        f"/v1/sandboxes/{session_id}/snapshots", json={}, headers={"Authorization": f"Bearer {key}"}
    )
    snapshot_id = create_resp.json()["id"]
    storage_key_prefix = create_resp.json()["storage_key_prefix"]

    delete_resp = await client.delete(f"/v1/snapshots/{snapshot_id}", headers={"Authorization": f"Bearer {key}"})
    assert delete_resp.status_code == 204
    assert storage_key_prefix in fake_snapshot_storage.delete_calls

    get_resp = await client.get(f"/v1/snapshots/{snapshot_id}", headers={"Authorization": f"Bearer {key}"})
    assert get_resp.status_code == 404

    # A repeat delete also 404s rather than erroring or silently succeeding.
    second_delete = await client.delete(f"/v1/snapshots/{snapshot_id}", headers={"Authorization": f"Bearer {key}"})
    assert second_delete.status_code == 404


async def test_delete_snapshot_storage_failure_leaves_row_intact(
    client: httpx.AsyncClient, fake_snapshot_storage: FakeSnapshotStorageClient
):
    """The design doc requires storage objects to actually be deleted, not
    just the DB row -- so a storage-side delete failure must not silently
    soft-delete the row and leave orphaned objects behind."""
    key = await signup_and_get_api_key(client, "snap-delete-fail@example.com")
    session_id = await _create_session_with_file(client, key)
    create_resp = await client.post(
        f"/v1/sandboxes/{session_id}/snapshots", json={}, headers={"Authorization": f"Bearer {key}"}
    )
    snapshot_id = create_resp.json()["id"]

    fake_snapshot_storage.fail_next_delete = True
    delete_resp = await client.delete(f"/v1/snapshots/{snapshot_id}", headers={"Authorization": f"Bearer {key}"})
    assert delete_resp.status_code == 502

    get_resp = await client.get(f"/v1/snapshots/{snapshot_id}", headers={"Authorization": f"Bearer {key}"})
    assert get_resp.status_code == 200


# ── Cross-tenant isolation -- the design doc's highest-severity risk ────


async def test_account_cannot_create_snapshot_of_another_accounts_session(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "snap-cross-a@example.com")
    key_b = await signup_and_get_api_key(client, "snap-cross-b@example.com")
    session_id = await _create_session_with_file(client, key_a)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/snapshots", json={}, headers={"Authorization": f"Bearer {key_b}"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_account_cannot_list_another_accounts_session_snapshots(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "snap-cross-list-a@example.com")
    key_b = await signup_and_get_api_key(client, "snap-cross-list-b@example.com")
    session_id = await _create_session_with_file(client, key_a)
    await client.post(f"/v1/sandboxes/{session_id}/snapshots", json={}, headers={"Authorization": f"Bearer {key_a}"})

    resp = await client.get(f"/v1/sandboxes/{session_id}/snapshots", headers={"Authorization": f"Bearer {key_b}"})
    assert resp.status_code == 404


async def test_account_cannot_get_another_accounts_snapshot(client: httpx.AsyncClient):
    key_a = await signup_and_get_api_key(client, "snap-cross-get-a@example.com")
    key_b = await signup_and_get_api_key(client, "snap-cross-get-b@example.com")
    session_id = await _create_session_with_file(client, key_a)
    create_resp = await client.post(
        f"/v1/sandboxes/{session_id}/snapshots", json={}, headers={"Authorization": f"Bearer {key_a}"}
    )
    snapshot_id = create_resp.json()["id"]

    resp = await client.get(f"/v1/snapshots/{snapshot_id}", headers={"Authorization": f"Bearer {key_b}"})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_account_cannot_restore_another_accounts_snapshot(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key_a = await signup_and_get_api_key(client, "snap-cross-restore-a@example.com")
    key_b = await signup_and_get_api_key(client, "snap-cross-restore-b@example.com")
    session_id = await _create_session_with_file(client, key_a)
    create_resp = await client.post(
        f"/v1/sandboxes/{session_id}/snapshots", json={}, headers={"Authorization": f"Bearer {key_a}"}
    )
    snapshot_id = create_resp.json()["id"]

    before = set(fake_manager.created)
    resp = await client.post(
        f"/v1/snapshots/{snapshot_id}/restore", json={}, headers={"Authorization": f"Bearer {key_b}"}
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"
    # No sandbox was ever created for the attacker's restore attempt.
    assert set(fake_manager.created) == before


async def test_account_cannot_delete_another_accounts_snapshot(
    client: httpx.AsyncClient, fake_snapshot_storage: FakeSnapshotStorageClient
):
    key_a = await signup_and_get_api_key(client, "snap-cross-delete-a@example.com")
    key_b = await signup_and_get_api_key(client, "snap-cross-delete-b@example.com")
    session_id = await _create_session_with_file(client, key_a)
    create_resp = await client.post(
        f"/v1/sandboxes/{session_id}/snapshots", json={}, headers={"Authorization": f"Bearer {key_a}"}
    )
    snapshot_id = create_resp.json()["id"]

    resp = await client.delete(f"/v1/snapshots/{snapshot_id}", headers={"Authorization": f"Bearer {key_b}"})
    assert resp.status_code == 404
    assert fake_snapshot_storage.delete_calls == []

    # A can still see and delete their own snapshot afterwards.
    get_as_a = await client.get(f"/v1/snapshots/{snapshot_id}", headers={"Authorization": f"Bearer {key_a}"})
    assert get_as_a.status_code == 200


async def test_restore_openapi_description_has_no_pricing_language(client: httpx.AsyncClient):
    """The restore route's OpenAPI `description` is public-facing (Swagger UI,
    generated SDK docs) but isn't a response body, so it isn't covered by
    _assert_no_pricing_language's usual call sites in test_usage_limits.py --
    check it directly here instead."""
    schema_resp = await client.get("/openapi.json")
    assert schema_resp.status_code == 200
    restore_op = schema_resp.json()["paths"]["/v1/snapshots/{snapshot_id}/restore"]["post"]
    _assert_no_pricing_language({"description": restore_op["description"]})


# The docs/API.md-reading counterpart of the OpenAPI check above (asserting
# docs/API.md's hand-written restore section has no pricing language too)
# lives in private/tests/test_api_md_pricing_language.py instead -- docs/
# and control-plane/ are no longer siblings after the public/private split
# (see docs/OSS-VS-HOSTED-SPLIT-POSITION.md).
