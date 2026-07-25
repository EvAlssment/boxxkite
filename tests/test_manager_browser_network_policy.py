"""Tests for SandboxManager's per-session browser-egress NetworkPolicy
wiring (docs/BROWSER-EXEC-DESIGN.md §3, GitHub issue #119):
BOXKITE_BROWSER_NETWORK_POLICY_ENABLED gating, provisioning at
session-configure time, and teardown at session-end (recycle-to-warm or
hard delete) so a reused pod never inherits a previous tenant's broad
browser-egress rule.

Mirrors tests/test_manager_secrets_network_policy.py's pattern exactly.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from boxkite.manager import SandboxManager
from boxkite.browser_network_policy import browser_egress_policy_name
from test_manager import _FakeCoreApi

pytestmark = pytest.mark.pr


class _FakeNetworkingApi:
    def __init__(self, *, replace_raises: Exception | None = None):
        self._replace_raises = replace_raises
        self.create_calls = []
        self.replace_calls = []
        self.delete_calls = []
        self._existing: dict[str, object] = {}

    async def replace_namespaced_network_policy(self, *, name, namespace, body):
        self.replace_calls.append({"name": name, "namespace": namespace, "body": body})
        if self._replace_raises is not None:
            raise self._replace_raises
        if name not in self._existing:
            raise ApiException(status=404)
        self._existing[name] = body
        return body

    async def create_namespaced_network_policy(self, *, namespace, body):
        self.create_calls.append({"namespace": namespace, "body": body})
        self._existing[body.metadata.name] = body
        return body

    async def delete_namespaced_network_policy(self, *, name, namespace):
        self.delete_calls.append({"name": name, "namespace": namespace})
        if name not in self._existing:
            raise ApiException(status=404)
        del self._existing[name]


def _manager_with_fake_networking(monkeypatch, *, replace_raises=None) -> tuple[SandboxManager, _FakeNetworkingApi]:
    manager = SandboxManager()
    fake_networking = _FakeNetworkingApi(replace_raises=replace_raises)
    manager._k8s_initialized = True
    manager._k8s_core_api = _FakeCoreApi()
    manager._k8s_networking_api = fake_networking
    # Same module-split subtlety test_manager_secrets_network_policy.py's
    # own helper comments -- the flag is read as a bare name inside
    # TlsAuthMixin's methods (_manager_tls_auth.py), which get their own
    # copy of it via that module's own `from ._manager_config import *`.
    monkeypatch.setattr("boxkite._manager_tls_auth.BOXKITE_BROWSER_NETWORK_POLICY_ENABLED", True)
    return manager, fake_networking


@pytest.mark.asyncio
async def test_sync_is_a_true_noop_when_feature_disabled(monkeypatch):
    """Default-off: this method must not touch K8s at all when the flag is
    unset, not even when browser_enabled=True."""
    manager = SandboxManager()
    monkeypatch.setattr("boxkite._manager_tls_auth.BOXKITE_BROWSER_NETWORK_POLICY_ENABLED", False)

    async def _fail_if_called():
        raise AssertionError("_init_k8s must not be called when the feature flag is off")

    manager._init_k8s = _fail_if_called

    await manager._sync_browser_egress_network_policy("pod-1", "session-1", True)


@pytest.mark.asyncio
async def test_delete_is_a_true_noop_when_feature_disabled(monkeypatch):
    manager = SandboxManager()
    monkeypatch.setattr("boxkite._manager_tls_auth.BOXKITE_BROWSER_NETWORK_POLICY_ENABLED", False)

    async def _fail_if_called():
        raise AssertionError("_init_k8s must not be called when the feature flag is off")

    manager._init_k8s = _fail_if_called

    await manager._delete_browser_egress_network_policy("pod-1")


@pytest.mark.asyncio
async def test_sync_creates_policy_when_browser_enabled_and_none_exists(monkeypatch):
    manager, fake_networking = _manager_with_fake_networking(monkeypatch)

    await manager._sync_browser_egress_network_policy("sandbox-pod-1", "session-abc", True)

    # replace attempted first (not found -> 404), then create.
    assert len(fake_networking.replace_calls) == 1
    assert fake_networking.replace_calls[0]["name"] == browser_egress_policy_name("sandbox-pod-1")
    assert len(fake_networking.create_calls) == 1
    created_body = fake_networking.create_calls[0]["body"]
    assert created_body.metadata.name == browser_egress_policy_name("sandbox-pod-1")
    assert created_body.spec.pod_selector.match_labels == {
        "app": "sandbox",
        "session-id": "session-abc",
    }


@pytest.mark.asyncio
async def test_sync_replaces_existing_policy_without_create(monkeypatch):
    manager, fake_networking = _manager_with_fake_networking(monkeypatch)
    # Pre-seed as if a prior session's policy already exists.
    await manager._sync_browser_egress_network_policy("sandbox-pod-2", "session-old", True)
    fake_networking.create_calls.clear()
    fake_networking.replace_calls.clear()

    # A NEW session claims the same pod (warm-pool reuse), also browser-enabled.
    await manager._sync_browser_egress_network_policy("sandbox-pod-2", "session-new", True)

    assert len(fake_networking.replace_calls) == 1
    assert len(fake_networking.create_calls) == 0
    replaced_body = fake_networking.replace_calls[0]["body"]
    assert replaced_body.spec.pod_selector.match_labels["session-id"] == "session-new"


@pytest.mark.asyncio
async def test_sync_deletes_policy_when_new_session_does_not_enable_browser(monkeypatch):
    """A warm-pool-claimed pod's PREVIOUS tenant had the browser tool
    enabled; the NEW tenant does not -- the stale broad-egress rule must be
    deleted, never left standing for a session that never asked for it."""
    manager, fake_networking = _manager_with_fake_networking(monkeypatch)
    # A policy exists from a prior session on this pod.
    await manager._sync_browser_egress_network_policy("sandbox-pod-3", "session-old", True)
    fake_networking.create_calls.clear()
    fake_networking.replace_calls.clear()

    # New session on the same pod does NOT have the browser tool enabled.
    await manager._sync_browser_egress_network_policy("sandbox-pod-3", "session-new", False)

    assert fake_networking.delete_calls[-1]["name"] == browser_egress_policy_name("sandbox-pod-3")
    assert len(fake_networking.create_calls) == 0
    assert len(fake_networking.replace_calls) == 0


@pytest.mark.asyncio
async def test_sync_swallows_replace_error_without_raising(monkeypatch):
    manager, fake_networking = _manager_with_fake_networking(
        monkeypatch, replace_raises=ApiException(status=500)
    )

    # Must not raise -- fails closed (no egress rule), not the session itself.
    await manager._sync_browser_egress_network_policy("sandbox-pod-4", "session-abc", True)
    assert fake_networking.create_calls == []


@pytest.mark.asyncio
async def test_sync_noop_when_networking_api_unavailable(monkeypatch):
    manager = SandboxManager()
    monkeypatch.setattr("boxkite._manager_tls_auth.BOXKITE_BROWSER_NETWORK_POLICY_ENABLED", True)

    async def fake_init_k8s():
        manager._k8s_networking_api = None

    manager._init_k8s = fake_init_k8s

    # Must not raise even though there's no networking API to call.
    await manager._sync_browser_egress_network_policy("sandbox-pod-5", "session-abc", True)


@pytest.mark.asyncio
async def test_delete_tolerates_already_absent_policy(monkeypatch):
    manager, fake_networking = _manager_with_fake_networking(monkeypatch)

    # No policy was ever created for this pod -- delete must not raise.
    await manager._delete_browser_egress_network_policy("never-had-one")
    assert fake_networking.delete_calls[-1]["name"] == browser_egress_policy_name("never-had-one")


@pytest.mark.asyncio
async def test_delete_pod_deletes_browser_egress_policy():
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    delete_calls = []

    async def fake_delete_secrets_egress_network_policy(pod_name):
        pass

    async def fake_delete_browser_egress_network_policy(pod_name):
        delete_calls.append(pod_name)

    manager._delete_secrets_egress_network_policy = fake_delete_secrets_egress_network_policy
    manager._delete_browser_egress_network_policy = fake_delete_browser_egress_network_policy

    await manager._delete_pod("sandbox-pod-hard-delete")

    assert delete_calls == ["sandbox-pod-hard-delete"]


@pytest.mark.asyncio
async def test_create_k8s_session_syncs_browser_egress_policy_with_session_label_and_flag(monkeypatch):
    """End-to-end (mocked K8s) check that _create_k8s_session calls the
    browser sync method with this session's exact session-id label value
    and its browser_enabled flag, after the pod's identity labels are
    patched -- mirrors
    test_create_k8s_session_syncs_secrets_egress_policy_with_session_label."""
    manager = SandboxManager()

    async def fake_init_k8s():
        return None

    async def fake_claim_warm_pod(size="small"):
        return None

    async def fake_create_pod(*_args, **_kwargs):
        return "10.8.0.99"

    fake_configure_response = SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"prefetched_files": []}
    )
    fake_client = SimpleNamespace(post=AsyncMock(return_value=fake_configure_response))

    sync_calls = []

    async def fake_sync(pod_name, session_label_value, browser_enabled):
        sync_calls.append((pod_name, session_label_value, browser_enabled))

    manager._init_k8s = fake_init_k8s
    manager._claim_warm_pod_via_k8s = fake_claim_warm_pod
    manager._create_pod = fake_create_pod
    manager._get_http_client = lambda *_args, **_kwargs: fake_client
    manager._k8s_core_api = SimpleNamespace(patch_namespaced_pod=AsyncMock())
    manager._sync_browser_egress_network_policy = fake_sync

    session_id = "session-browser-e2e"
    await manager._create_k8s_session(
        uuid4(),
        session_id,
        None,
        None,
        browser_enabled=True,
    )

    assert len(sync_calls) == 1
    pod_name, session_label_value, browser_enabled = sync_calls[0]
    assert session_label_value.startswith("session-")
    assert browser_enabled is True


@pytest.mark.asyncio
async def test_create_k8s_session_defaults_browser_enabled_to_false(monkeypatch):
    manager = SandboxManager()

    async def fake_init_k8s():
        return None

    async def fake_claim_warm_pod(size="small"):
        return None

    async def fake_create_pod(*_args, **_kwargs):
        return "10.8.0.99"

    fake_configure_response = SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"prefetched_files": []}
    )
    fake_client = SimpleNamespace(post=AsyncMock(return_value=fake_configure_response))

    sync_calls = []

    async def fake_sync(pod_name, session_label_value, browser_enabled):
        sync_calls.append((pod_name, session_label_value, browser_enabled))

    manager._init_k8s = fake_init_k8s
    manager._claim_warm_pod_via_k8s = fake_claim_warm_pod
    manager._create_pod = fake_create_pod
    manager._get_http_client = lambda *_args, **_kwargs: fake_client
    manager._k8s_core_api = SimpleNamespace(patch_namespaced_pod=AsyncMock())
    manager._sync_browser_egress_network_policy = fake_sync

    await manager._create_k8s_session(uuid4(), "session-default", None, None)

    assert sync_calls[0][2] is False


@pytest.mark.asyncio
async def test_create_session_rejects_browser_enabled_with_small_size_before_touching_k8s():
    """Integration-level check on the actual public entry point (not just
    the isolated _validate_browser_resource_floor unit test in
    test_browser_resource_floor.py): a caller requesting browser_enabled=True
    with the default 'small' size must get ValueError immediately, before
    any warm-pool claim, K8s API call, or session-create lock is touched."""
    manager = SandboxManager()

    async def _fail_if_called(*_args, **_kwargs):
        raise AssertionError("must not reach K8s/warm-pool when size validation fails first")

    manager._init_k8s = _fail_if_called
    manager._claim_warm_pod_via_k8s = _fail_if_called
    manager._create_pod = _fail_if_called

    with pytest.raises(ValueError, match="browser_enabled=True requires size='medium' or 'large'"):
        await manager.create_session(
            organization_id=uuid4(),
            session_id="session-browser-small-reject",
            browser_enabled=True,
            size="small",
        )
