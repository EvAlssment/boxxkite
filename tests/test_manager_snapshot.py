"""SandboxManager.snapshot() and the restore_from_snapshot_id annotation
plumbed through create_session -- docs/SNAPSHOT-DESIGN.md.

Mirrors test_manager.py's existing style: monkeypatch `_resolve_session`/
`_get_http_client` rather than a real sidecar or K8s API.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from boxkite.manager import RESTORED_FROM_SNAPSHOT_ANNOTATION, SandboxManager


@pytest.mark.asyncio
async def test_snapshot_calls_confirmed_flush_endpoint_and_returns_manifest():
    manager = SandboxManager()
    session_id = "session-1"

    async def fake_resolve_session(target_session_id: str):
        assert target_session_id == session_id
        return ("sandbox-pod", "10.8.0.80")

    fake_response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {
            "status": "flushed",
            "storage_prefix": "sessions/org-1/session-1",
            "storage_keys": ["workspace/foo.py", "outputs/report.pdf"],
        },
    )
    fake_client = SimpleNamespace(post=AsyncMock(return_value=fake_response))

    manager._resolve_session = fake_resolve_session
    manager._get_http_client = lambda *_args, **_kwargs: fake_client

    result = await manager.snapshot(session_id)

    fake_client.post.assert_awaited_once_with("/flush/confirmed")
    assert result == {
        "status": "flushed",
        "storage_prefix": "sessions/org-1/session-1",
        "storage_keys": ["workspace/foo.py", "outputs/report.pdf"],
    }


@pytest.mark.asyncio
async def test_snapshot_propagates_http_errors():
    manager = SandboxManager()
    session_id = "session-1"

    async def fake_resolve_session(target_session_id: str):
        return ("sandbox-pod", "10.8.0.80")

    def _raise():
        raise RuntimeError("sidecar unreachable")

    fake_response = SimpleNamespace(raise_for_status=_raise)
    fake_client = SimpleNamespace(post=AsyncMock(return_value=fake_response))

    manager._resolve_session = fake_resolve_session
    manager._get_http_client = lambda *_args, **_kwargs: fake_client
    manager._is_retryable_sidecar_error = lambda _exc: False

    with pytest.raises(RuntimeError, match="sidecar unreachable"):
        await manager.snapshot(session_id)


@pytest.mark.asyncio
async def test_create_k8s_session_records_restore_from_snapshot_annotation(monkeypatch):
    manager = SandboxManager()

    async def fake_init_k8s():
        return None

    async def fake_claim_warm_pod(size="small"):
        return None

    async def fake_create_pod(*_args, **_kwargs):
        return "10.8.0.99"

    fake_configure_response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"prefetched_files": []},
    )
    fake_client = SimpleNamespace(post=AsyncMock(return_value=fake_configure_response))

    patch_calls: list[dict] = []

    async def fake_patch_namespaced_pod(*, name, namespace, body):
        patch_calls.append(body)

    manager._init_k8s = fake_init_k8s
    manager._claim_warm_pod_via_k8s = fake_claim_warm_pod
    manager._create_pod = fake_create_pod
    manager._get_http_client = lambda *_args, **_kwargs: fake_client
    manager._k8s_core_api = SimpleNamespace(patch_namespaced_pod=fake_patch_namespaced_pod)

    from uuid import uuid4

    await manager._create_k8s_session(
        uuid4(),
        "session-restored",
        None,
        None,
        restore_from_snapshot_id="snap-123",
    )

    assert patch_calls, "expected the pod-metadata patch to have been issued"
    annotations = patch_calls[0]["metadata"]["annotations"]
    assert annotations[RESTORED_FROM_SNAPSHOT_ANNOTATION] == "snap-123"


@pytest.mark.asyncio
async def test_create_k8s_session_without_restore_records_empty_annotation(monkeypatch):
    manager = SandboxManager()

    async def fake_init_k8s():
        return None

    async def fake_claim_warm_pod(size="small"):
        return None

    async def fake_create_pod(*_args, **_kwargs):
        return "10.8.0.99"

    fake_configure_response = SimpleNamespace(
        raise_for_status=lambda: None,
        json=lambda: {"prefetched_files": []},
    )
    fake_client = SimpleNamespace(post=AsyncMock(return_value=fake_configure_response))

    patch_calls: list[dict] = []

    async def fake_patch_namespaced_pod(*, name, namespace, body):
        patch_calls.append(body)

    manager._init_k8s = fake_init_k8s
    manager._claim_warm_pod_via_k8s = fake_claim_warm_pod
    manager._create_pod = fake_create_pod
    manager._get_http_client = lambda *_args, **_kwargs: fake_client
    manager._k8s_core_api = SimpleNamespace(patch_namespaced_pod=fake_patch_namespaced_pod)

    from uuid import uuid4

    await manager._create_k8s_session(uuid4(), "session-fresh", None, None)

    annotations = patch_calls[0]["metadata"]["annotations"]
    assert annotations[RESTORED_FROM_SNAPSHOT_ANNOTATION] == ""
