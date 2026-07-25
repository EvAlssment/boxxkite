"""Idempotency-Key middleware: retries replay the first response instead of
creating duplicate resources (see control_plane/idempotency.py)."""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, signup_and_get_api_key


async def test_retry_with_same_key_replays_and_creates_once(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "idem-replay@example.com")
    headers = {"Authorization": f"Bearer {key}", "Idempotency-Key": "req-abc-123"}

    first = await client.post("/v1/sandboxes", json={"label": "x"}, headers=headers)
    assert first.status_code == 201

    second = await client.post("/v1/sandboxes", json={"label": "x"}, headers=headers)
    assert second.status_code == 201
    # Same resource returned, flagged as a replay, and only ONE real create.
    assert second.json()["id"] == first.json()["id"]
    assert second.headers.get("idempotent-replayed") == "true"
    assert len(fake_manager.created) == 1


async def test_same_key_different_body_is_rejected(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "idem-mismatch@example.com")
    headers = {"Authorization": f"Bearer {key}", "Idempotency-Key": "req-def-456"}

    first = await client.post("/v1/sandboxes", json={"label": "x"}, headers=headers)
    assert first.status_code == 201

    conflict = await client.post("/v1/sandboxes", json={"label": "different"}, headers=headers)
    assert conflict.status_code == 422
    assert conflict.json()["error"]["code"] == "idempotency_key_reuse"
    assert len(fake_manager.created) == 1


async def test_no_key_is_a_passthrough(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "idem-passthrough@example.com")
    resp = await client.post(
        "/v1/sandboxes", json={"label": "x"}, headers={"Authorization": f"Bearer {key}"}
    )
    assert resp.status_code == 201
    assert "idempotent-replayed" not in resp.headers


async def test_same_key_different_accounts_do_not_collide(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    # The same opaque key from two different callers must not cross-wire.
    key_a = await signup_and_get_api_key(client, "idem-a@example.com")
    key_b = await signup_and_get_api_key(client, "idem-b@example.com")
    shared = {"Idempotency-Key": "same-key-value"}

    ra = await client.post(
        "/v1/sandboxes", json={"label": "a"}, headers={"Authorization": f"Bearer {key_a}", **shared}
    )
    rb = await client.post(
        "/v1/sandboxes", json={"label": "b"}, headers={"Authorization": f"Bearer {key_b}", **shared}
    )
    assert ra.status_code == 201
    assert rb.status_code == 201
    assert ra.json()["id"] != rb.json()["id"]
    assert len(fake_manager.created) == 2
