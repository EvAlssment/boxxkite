"""SandboxManager.create_session's image_ref plumbing --
docs/DECLARATIVE-BUILDER-DESIGN.md.

Mirrors test_manager_snapshot.py's style: monkeypatch `_resolve_session`/
`_get_http_client`/`_k8s_core_api` rather than a real sidecar or K8s API.

Covers the two load-bearing guarantees from the design doc's security
section:
1. image_ref must be a digest-pinned reference (repo@sha256:<64-hex>) --
   anything else (a bare tag, a malformed digest) is rejected outright.
2. The pod's security_context, resources, and volume mounts are IDENTICAL
   regardless of whether a custom image_ref is supplied -- the referenced
   image must never be able to influence the security posture of the pod
   it runs in.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from boxkite.manager import SANDBOX_IMAGE, SandboxManager, _validate_image_ref
from test_manager import _FakeCoreApi, _container_by_name


def test_validate_image_ref_accepts_pinned_digest():
    ref = "registry.internal/boxkite-images/acct-1/img-1@sha256:" + "a" * 64
    assert _validate_image_ref(ref) == ref


def test_validate_image_ref_accepts_none():
    assert _validate_image_ref(None) is None


@pytest.mark.parametrize(
    "bad_ref",
    [
        "boxkite-sandbox:latest",  # mutable tag, no digest at all
        "registry.internal/boxkite-images/acct-1/img-1:latest",  # tag, not digest
        "registry.internal/boxkite-images/acct-1/img-1@sha256:short",  # too-short digest
        "registry.internal/boxkite-images/acct-1/img-1@md5:" + "a" * 32,  # wrong algo
        "",
    ],
)
def test_validate_image_ref_rejects_non_digest_references(bad_ref):
    with pytest.raises(ValueError, match="digest-pinned"):
        _validate_image_ref(bad_ref)


@pytest.mark.asyncio
async def test_create_session_rejects_non_digest_image_ref():
    manager = SandboxManager()
    with pytest.raises(ValueError, match="digest-pinned"):
        await manager.create_session(
            organization_id=None,
            session_id="session-bad-image",
            image_ref="not-a-digest-ref:latest",
        )


@pytest.mark.asyncio
async def test_create_pod_uses_image_ref_when_given_and_sandbox_image_otherwise(monkeypatch):
    manager = SandboxManager()

    captured_images: list[str] = []

    async def fake_init_k8s():
        return None

    manager._init_k8s = fake_init_k8s

    async def fake_create_pod_call(pod_name, session_id, organization_id, work_item_id, **kwargs):
        # Reproduce just enough of the real _create_pod's image-selection
        # logic to assert the invariant without needing the full K8s client
        # plumbing (client.V1Pod etc. require the `kubernetes` package's
        # object model, which this unit test intentionally avoids).
        from boxkite.manager import SANDBOX_IMAGE

        image_ref = kwargs.get("image_ref")
        captured_images.append(image_ref or SANDBOX_IMAGE)
        return "10.8.0.5"

    manager._create_pod = fake_create_pod_call

    from uuid import uuid4

    async def fake_claim_warm_pod(size="small"):
        return None

    fake_configure_response = SimpleNamespace(raise_for_status=lambda: None, json=lambda: {"prefetched_files": []})
    fake_client = SimpleNamespace(post=AsyncMock(return_value=fake_configure_response))
    manager._claim_warm_pod_via_k8s = fake_claim_warm_pod
    manager._get_http_client = lambda *_args, **_kwargs: fake_client
    manager._k8s_core_api = SimpleNamespace(patch_namespaced_pod=AsyncMock())

    pinned = "registry.internal/boxkite-images/acct-1/img-1@sha256:" + "b" * 64
    await manager._create_k8s_session(uuid4(), "session-custom-image", None, None, image_ref=pinned)
    await manager._create_k8s_session(uuid4(), "session-default-image", None, None, image_ref=None)

    assert captured_images[0] == pinned

    from boxkite.manager import SANDBOX_IMAGE

    assert captured_images[1] == SANDBOX_IMAGE


@pytest.mark.asyncio
async def test_custom_image_ref_does_not_change_pod_security_context_or_resources(monkeypatch):
    """docs/DECLARATIVE-BUILDER-DESIGN.md section 5: "the pod's security
    context must never be a function of the referenced image." Exercises
    the REAL `_create_pod` (via the same `_FakeCoreApi`/`client.V1Pod`
    machinery test_manager.py already uses, not a stub), and asserts the
    sandbox container's security_context/resources/volume_mounts are
    byte-for-byte identical whether image_ref is a custom digest or None --
    only the `image` field itself may differ."""
    manager_custom = SandboxManager()
    manager_custom._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.91"

    monkeypatch.setattr(manager_custom, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    pinned = "registry.internal/boxkite-images/acct-1/img-1@sha256:" + "d" * 64
    await manager_custom._create_pod(
        pod_name="sandbox-custom-image",
        session_id="session-custom-image",
        organization_id=uuid4(),
        work_item_id=uuid4(),
        image_ref=pinned,
    )

    manager_default = SandboxManager()
    manager_default._k8s_core_api = _FakeCoreApi()
    monkeypatch.setattr(manager_default, "_wait_for_pod_ready", fake_wait_for_pod_ready)
    await manager_default._create_pod(
        pod_name="sandbox-default-image",
        session_id="session-default-image",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )

    custom_pod = manager_custom._k8s_core_api.create_calls[0]["body"]
    default_pod = manager_default._k8s_core_api.create_calls[0]["body"]

    custom_sandbox = _container_by_name(custom_pod, "sandbox")
    default_sandbox = _container_by_name(default_pod, "sandbox")

    assert custom_sandbox.image == pinned
    assert default_sandbox.image == SANDBOX_IMAGE
    assert custom_sandbox.image != default_sandbox.image

    # Everything else about the sandbox container must be identical.
    assert custom_sandbox.security_context == default_sandbox.security_context
    assert custom_sandbox.resources == default_sandbox.resources
    assert custom_sandbox.volume_mounts == default_sandbox.volume_mounts
    assert custom_pod.spec.share_process_namespace == default_pod.spec.share_process_namespace
    assert custom_pod.spec.service_account_name == default_pod.spec.service_account_name
    assert custom_pod.spec.restart_policy == default_pod.spec.restart_policy

    custom_sidecar = _container_by_name(custom_pod, "sidecar")
    default_sidecar = _container_by_name(default_pod, "sidecar")
    assert custom_sidecar.security_context == default_sidecar.security_context
    assert custom_sidecar.resources == default_sidecar.resources


@pytest.mark.asyncio
async def test_custom_image_forces_cold_create_bypassing_warm_pool(monkeypatch):
    """docs/DECLARATIVE-BUILDER-DESIGN.md section 4's recommendation (a):
    WarmPoolManager only ever pre-warms the operator's default image, so a
    custom image_ref must never claim a warm pod (which would silently run
    the wrong image)."""
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

    from uuid import uuid4

    pinned = "registry.internal/boxkite-images/acct-1/img-1@sha256:" + "c" * 64
    await manager._create_k8s_session(uuid4(), "session-forces-cold", None, None, image_ref=pinned)

    assert claim_called is False, "a custom image_ref must never claim a warm pod"
