"""Opt-in GPU support at the control-plane API layer
(docs/GPU-SUPPORT-SCOPING.md) -- POST /v1/sandboxes' gpu_count field.

Only the API-layer gating/threading is covered here (this repo has no live
GPU-equipped cluster to test scheduling against) -- see
tests/test_gpu_support.py for resource_config.py's own unit coverage.
"""

from __future__ import annotations

import httpx

from conftest import signup_and_get_api_key
from boxkite import resource_config


async def test_gpu_count_rejected_when_gpu_support_disabled(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(resource_config, "gpu_enabled", lambda: False)
    api_key = await signup_and_get_api_key(client, "gpu-disabled@example.com")

    resp = await client.post(
        "/v1/sandboxes",
        json={"gpu_count": 1},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "gpu_support_disabled"


async def test_gpu_count_accepted_and_threaded_to_manager_when_enabled(
    client: httpx.AsyncClient, monkeypatch, fake_manager
):
    monkeypatch.setattr(resource_config, "gpu_enabled", lambda: True)
    monkeypatch.setattr(resource_config, "max_gpu_count_per_session", lambda: 4)
    api_key = await signup_and_get_api_key(client, "gpu-enabled@example.com")

    resp = await client.post(
        "/v1/sandboxes",
        json={"gpu_count": 2},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    assert fake_manager.created[session_id]["gpu_count"] == 2


async def test_gpu_count_rejected_above_ceiling(client: httpx.AsyncClient, monkeypatch):
    monkeypatch.setattr(resource_config, "gpu_enabled", lambda: True)
    monkeypatch.setattr(resource_config, "max_gpu_count_per_session", lambda: 1)
    api_key = await signup_and_get_api_key(client, "gpu-ceiling@example.com")

    resp = await client.post(
        "/v1/sandboxes",
        json={"gpu_count": 2},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "invalid_gpu_count"


async def test_gpu_count_omitted_by_default_is_unaffected(client: httpx.AsyncClient, fake_manager):
    """No gpu_count in the request at all -- ordinary sandbox creation,
    completely unaffected by whether GPU support is enabled."""
    api_key = await signup_and_get_api_key(client, "gpu-omitted@example.com")

    resp = await client.post(
        "/v1/sandboxes",
        json={},
        headers={"Authorization": f"Bearer {api_key}"},
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["id"]
    assert fake_manager.created[session_id]["gpu_count"] is None
