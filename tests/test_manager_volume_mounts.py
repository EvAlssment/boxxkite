"""SandboxManager.create_session's volume_mounts plumbing --
docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md's Volume addendum.

Mirrors test_manager_declarative_builder.py's style and structure exactly
(same _FakeCoreApi/client.V1Pod machinery, not stubs) for the equivalent
guarantees:
1. A malformed volume_mounts entry (missing pvc_name, a mount_path
   colliding with a reserved root) is rejected outright.
2. A valid volume_mounts entry adds exactly one PVC-backed V1Volume/
   V1VolumeMount pair to the pod and the `sandbox` container ONLY --
   the sidecar container's volume_mounts are unaffected, and every other
   sandbox-container field (security_context, resources) is unchanged.
3. A non-empty volume_mounts forces a cold pod create, same as image_ref
   (no pre-warmed pod has any PVC premounted).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from boxkite.manager import SandboxManager, _validate_volume_mounts
from test_manager import _FakeCoreApi, _container_by_name


def test_validate_volume_mounts_accepts_none():
    assert _validate_volume_mounts(None) is None


def test_validate_volume_mounts_accepts_a_valid_entry():
    entries = [{"pvc_name": "boxkite-vol-abc123", "mount_path": "/data"}]
    assert _validate_volume_mounts(entries) == entries


def test_validate_volume_mounts_rejects_missing_pvc_name():
    with pytest.raises(ValueError, match="pvc_name"):
        _validate_volume_mounts([{"mount_path": "/data"}])


@pytest.mark.parametrize(
    "bad_path",
    ["/workspace", "/workspace/sub", "/mnt/user-data/outputs", "/tmp", "/", "relative/path", ""],
)
def test_validate_volume_mounts_rejects_reserved_or_invalid_paths(bad_path):
    with pytest.raises(ValueError):
        _validate_volume_mounts([{"pvc_name": "boxkite-vol-abc123", "mount_path": bad_path}])


@pytest.mark.asyncio
async def test_volume_mounts_add_a_pvc_volume_and_mount_to_the_sandbox_container_only(monkeypatch):
    manager_with_volume = SandboxManager()
    manager_with_volume._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.91"

    monkeypatch.setattr(manager_with_volume, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager_with_volume._create_pod(
        pod_name="sandbox-with-volume",
        session_id="session-with-volume",
        organization_id=uuid4(),
        work_item_id=uuid4(),
        volume_mounts=[{"pvc_name": "boxkite-vol-real-pvc", "mount_path": "/data"}],
    )

    manager_default = SandboxManager()
    manager_default._k8s_core_api = _FakeCoreApi()
    monkeypatch.setattr(manager_default, "_wait_for_pod_ready", fake_wait_for_pod_ready)
    await manager_default._create_pod(
        pod_name="sandbox-default",
        session_id="session-default",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )

    with_volume_pod = manager_with_volume._k8s_core_api.create_calls[0]["body"]
    default_pod = manager_default._k8s_core_api.create_calls[0]["body"]

    with_volume_sandbox = _container_by_name(with_volume_pod, "sandbox")
    default_sandbox = _container_by_name(default_pod, "sandbox")

    # Exactly one extra volume_mount on the sandbox container.
    assert len(with_volume_sandbox.volume_mounts) == len(default_sandbox.volume_mounts) + 1
    extra_mount = with_volume_sandbox.volume_mounts[-1]
    assert extra_mount.mount_path == "/data"

    # The matching pod-level volume references the real PVC name.
    matching_volumes = [v for v in with_volume_pod.spec.volumes if v.name == extra_mount.name]
    assert len(matching_volumes) == 1
    assert matching_volumes[0].persistent_volume_claim.claim_name == "boxkite-vol-real-pvc"

    # Nothing else about the sandbox container changed.
    assert with_volume_sandbox.security_context == default_sandbox.security_context
    assert with_volume_sandbox.resources == default_sandbox.resources
    assert with_volume_sandbox.image == default_sandbox.image

    # The sidecar container is completely unaffected.
    with_volume_sidecar = _container_by_name(with_volume_pod, "sidecar")
    default_sidecar = _container_by_name(default_pod, "sidecar")
    assert with_volume_sidecar.volume_mounts == default_sidecar.volume_mounts


@pytest.mark.asyncio
async def test_volume_mounts_forces_cold_create_bypassing_warm_pool():
    """No pre-warmed pod has any PVC premounted -- same
    forces_cold_create reasoning as image_ref."""
    manager = SandboxManager()

    claim_called = False

    async def fake_init_k8s():
        return None

    async def fake_claim_warm_pod(size="small"):
        nonlocal claim_called
        claim_called = True
        return ("warm-pod", "10.8.0.9")

    async def fake_create_pod(*_args, **_kwargs):
        return "10.8.0.99"

    fake_configure_response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"prefetched_files": []})
    fake_client = SimpleNamespace(post=AsyncMock(return_value=fake_configure_response))

    manager._init_k8s = fake_init_k8s
    manager._claim_warm_pod_via_k8s = fake_claim_warm_pod
    manager._create_pod = fake_create_pod
    manager._get_http_client = lambda *_args, **_kwargs: fake_client
    manager._k8s_core_api = SimpleNamespace(patch_namespaced_pod=AsyncMock())

    await manager._create_k8s_session(
        uuid4(),
        "session-with-volume",
        None,
        None,
        volume_mounts=[{"pvc_name": "boxkite-vol-x", "mount_path": "/data"}],
    )

    assert claim_called is False
