"""Tests for the public, unauthenticated demo playground
(issue #103, routers/demo_playground.py).

Covers: the happy-path create -> exec -> destroy flow; 404 when the feature
flag is off; 429 once BOXKITE_DEMO_RATE_LIMIT_PER_MINUTE is exceeded; 503
once BOXKITE_DEMO_MAX_CONCURRENT demo sessions are active; and token
rejection for a missing, mismatched, or expired X-Demo-Token on /exec.
"""

from __future__ import annotations

import httpx
import pytest

from conftest import FakeSandboxManager
from control_plane.config import settings
from control_plane.security import create_demo_session_token


@pytest.fixture(autouse=True)
def _enable_demo_playground(monkeypatch):
    # Off by default (opt-in feature) -- every test in this file explicitly
    # turns it on, mirroring test_sandbox_images.py's
    # _enable_image_builder fixture.
    monkeypatch.setattr(settings, "BOXKITE_DEMO_PLAYGROUND_ENABLED", True)


async def _create_demo_sandbox(client: httpx.AsyncClient) -> dict:
    resp = await client.post("/v1/demo/sandboxes", json={})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def test_create_exec_destroy_happy_path(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    created = await _create_demo_sandbox(client)
    assert created["session_id"]
    assert created["token"]
    assert created["expires_at"]

    exec_resp = await client.post(
        f"/v1/demo/sandboxes/{created['session_id']}/exec",
        json={"command": "echo hello"},
        headers={"X-Demo-Token": created["token"]},
    )
    assert exec_resp.status_code == 200, exec_resp.text
    body = exec_resp.json()
    assert body["exit_code"] == 0
    assert "echo hello" in body["stdout"]
    assert body["truncated"] is False
    assert fake_manager.exec_calls[-1]["timeout"] == settings.BOXKITE_DEMO_EXEC_TIMEOUT_SECONDS

    destroy_resp = await client.delete(
        f"/v1/demo/sandboxes/{created['session_id']}",
        headers={"X-Demo-Token": created["token"]},
    )
    assert destroy_resp.status_code == 204
    assert created["session_id"] in fake_manager.destroyed

    # Destroying again (e.g. a duplicate sendBeacon call) is a no-op, not
    # an error -- idempotent by design.
    destroy_again = await client.delete(
        f"/v1/demo/sandboxes/{created['session_id']}",
        headers={"X-Demo-Token": created["token"]},
    )
    assert destroy_again.status_code == 204

    # A destroyed session's own session_id/token no longer works for exec.
    exec_after_destroy = await client.post(
        f"/v1/demo/sandboxes/{created['session_id']}/exec",
        json={"command": "echo hi"},
        headers={"X-Demo-Token": created["token"]},
    )
    assert exec_after_destroy.status_code == 404


async def test_disabled_by_default(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_DEMO_PLAYGROUND_ENABLED", False)
    resp = await client.post("/v1/demo/sandboxes", json={})
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "not_found"


async def test_rate_limit_returns_429_after_exceeding_limit(client: httpx.AsyncClient, monkeypatch):
    # Isolate the rate-limit behavior from the capacity check by giving
    # capacity plenty of headroom.
    monkeypatch.setattr(settings, "BOXKITE_DEMO_MAX_CONCURRENT", 100)
    monkeypatch.setattr(settings, "BOXKITE_DEMO_RATE_LIMIT_PER_MINUTE", 2)

    first = await client.post("/v1/demo/sandboxes", json={})
    assert first.status_code == 201, first.text
    second = await client.post("/v1/demo/sandboxes", json={})
    assert second.status_code == 201, second.text

    third = await client.post("/v1/demo/sandboxes", json={})
    assert third.status_code == 429, third.text
    assert third.json()["detail"]["error"]["code"] == "rate_limited"


async def test_capacity_returns_503_after_max_concurrent_reached(client: httpx.AsyncClient, monkeypatch):
    # Isolate the capacity check from the rate limiter by giving the rate
    # limiter plenty of headroom.
    monkeypatch.setattr(settings, "BOXKITE_DEMO_RATE_LIMIT_PER_MINUTE", 100)
    monkeypatch.setattr(settings, "BOXKITE_DEMO_MAX_CONCURRENT", 1)

    first = await client.post("/v1/demo/sandboxes", json={})
    assert first.status_code == 201, first.text

    second = await client.post("/v1/demo/sandboxes", json={})
    assert second.status_code == 503, second.text
    assert second.json()["error"]["code"] == "demo_at_capacity"


async def test_exec_rejects_missing_token(client: httpx.AsyncClient):
    created = await _create_demo_sandbox(client)
    resp = await client.post(
        f"/v1/demo/sandboxes/{created['session_id']}/exec",
        json={"command": "echo hi"},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "missing_credentials"


async def test_exec_rejects_mismatched_session_token(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(settings, "BOXKITE_DEMO_MAX_CONCURRENT", 100)
    session_a = await _create_demo_sandbox(client)
    session_b = await _create_demo_sandbox(client)

    resp = await client.post(
        f"/v1/demo/sandboxes/{session_a['session_id']}/exec",
        json={"command": "echo hi"},
        headers={"X-Demo-Token": session_b["token"]},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


async def test_exec_rejects_expired_token(client: httpx.AsyncClient):
    created = await _create_demo_sandbox(client)
    expired_token, _expires_at = create_demo_session_token(session_id=created["session_id"], ttl_seconds=-1)

    resp = await client.post(
        f"/v1/demo/sandboxes/{created['session_id']}/exec",
        json={"command": "echo hi"},
        headers={"X-Demo-Token": expired_token},
    )
    assert resp.status_code == 401
    assert resp.json()["error"]["code"] == "invalid_token"


async def test_exec_output_is_truncated(client: httpx.AsyncClient, fake_manager: FakeSandboxManager, monkeypatch):
    from control_plane.schemas import DEMO_EXEC_OUTPUT_MAX_LENGTH

    created = await _create_demo_sandbox(client)

    huge_stdout = "x" * (DEMO_EXEC_OUTPUT_MAX_LENGTH + 500)

    async def _fake_execute(session_id, command, timeout=30, description=None):
        return {"exit_code": 0, "stdout": huge_stdout, "stderr": ""}

    monkeypatch.setattr(fake_manager, "execute", _fake_execute)

    resp = await client.post(
        f"/v1/demo/sandboxes/{created['session_id']}/exec",
        json={"command": "yes"},
        headers={"X-Demo-Token": created["token"]},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["stdout"]) == DEMO_EXEC_OUTPUT_MAX_LENGTH
    assert body["truncated"] is True


async def test_demo_sandbox_uses_default_image_no_extras(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    created = await _create_demo_sandbox(client)
    manager_call = fake_manager.created[created["session_id"]]
    assert manager_call["image_ref"] is None
    assert manager_call["volume_mounts"] is None
    assert manager_call["size"] == "small"
