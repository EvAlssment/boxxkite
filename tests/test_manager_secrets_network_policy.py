"""Tests for SandboxManager's per-session secrets-egress NetworkPolicy
wiring (issue #74): BOXKITE_SECRETS_NETWORK_POLICY_ENABLED gating,
provisioning at session-configure time, and teardown at session-end
(recycle-to-warm or hard delete) so a reused pod never inherits a previous
tenant's egress rule.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from boxkite.manager import SandboxManager
from boxkite.secrets_network_policy import secrets_egress_policy_name
from test_manager import _FakeCoreApi


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
    # The flag is read as a bare name inside TlsAuthMixin's methods, which
    # live in _manager_tls_auth.py and get their own copy of it via that
    # module's own `from ._manager_config import *` -- patching
    # boxkite.manager's copy (a separate binding from the same mechanical
    # split) would not be visible there.
    monkeypatch.setattr("boxkite._manager_tls_auth.BOXKITE_SECRETS_NETWORK_POLICY_ENABLED", True)
    monkeypatch.setattr(
        "boxkite.secrets_network_policy.default_resolve_host_ips",
        lambda host: ["93.184.216.40"],
    )
    return manager, fake_networking


SAMPLE_GRANTS = [{"name": "stripe-key", "allowed_hosts": ["api.stripe.com"]}]


@pytest.mark.asyncio
async def test_sync_is_a_true_noop_when_feature_disabled(monkeypatch):
    """Default-off: this method must not touch K8s at all when the flag is
    unset, not even to check secret_grants."""
    manager = SandboxManager()
    monkeypatch.setattr("boxkite._manager_tls_auth.BOXKITE_SECRETS_NETWORK_POLICY_ENABLED", False)

    async def _fail_if_called():
        raise AssertionError("_init_k8s must not be called when the feature flag is off")

    manager._init_k8s = _fail_if_called

    await manager._sync_secrets_egress_network_policy("pod-1", "session-1", SAMPLE_GRANTS)
    # No exception means _init_k8s (and therefore any K8s API call) was
    # never reached.


@pytest.mark.asyncio
async def test_delete_is_a_true_noop_when_feature_disabled(monkeypatch):
    manager = SandboxManager()
    monkeypatch.setattr("boxkite._manager_tls_auth.BOXKITE_SECRETS_NETWORK_POLICY_ENABLED", False)

    async def _fail_if_called():
        raise AssertionError("_init_k8s must not be called when the feature flag is off")

    manager._init_k8s = _fail_if_called

    await manager._delete_secrets_egress_network_policy("pod-1")


@pytest.mark.asyncio
async def test_sync_creates_policy_when_none_exists(monkeypatch):
    manager, fake_networking = _manager_with_fake_networking(monkeypatch)

    await manager._sync_secrets_egress_network_policy("sandbox-pod-1", "session-abc", SAMPLE_GRANTS)

    # replace attempted first (not found -> 404), then create.
    assert len(fake_networking.replace_calls) == 1
    assert fake_networking.replace_calls[0]["name"] == secrets_egress_policy_name("sandbox-pod-1")
    assert len(fake_networking.create_calls) == 1
    created_body = fake_networking.create_calls[0]["body"]
    assert created_body.metadata.name == secrets_egress_policy_name("sandbox-pod-1")
    assert created_body.spec.pod_selector.match_labels == {
        "app": "sandbox",
        "session-id": "session-abc",
    }


@pytest.mark.asyncio
async def test_sync_replaces_existing_policy_without_create(monkeypatch):
    manager, fake_networking = _manager_with_fake_networking(monkeypatch, replace_raises=None)
    # Pre-seed as if a prior session's policy already exists.
    await manager._sync_secrets_egress_network_policy("sandbox-pod-2", "session-old", SAMPLE_GRANTS)
    fake_networking.create_calls.clear()
    fake_networking.replace_calls.clear()

    # A NEW session claims the same pod (warm-pool reuse) with different grants.
    new_grants = [{"name": "github-token", "allowed_hosts": ["api.github.com"]}]
    await manager._sync_secrets_egress_network_policy("sandbox-pod-2", "session-new", new_grants)

    assert len(fake_networking.replace_calls) == 1
    assert len(fake_networking.create_calls) == 0
    replaced_body = fake_networking.replace_calls[0]["body"]
    assert replaced_body.spec.pod_selector.match_labels["session-id"] == "session-new"


@pytest.mark.asyncio
async def test_sync_deletes_policy_when_no_secret_grants(monkeypatch):
    manager, fake_networking = _manager_with_fake_networking(monkeypatch)
    # A policy exists from a prior session on this pod.
    await manager._sync_secrets_egress_network_policy("sandbox-pod-3", "session-old", SAMPLE_GRANTS)
    fake_networking.create_calls.clear()
    fake_networking.replace_calls.clear()

    # New session on the same pod was granted NO secrets.
    await manager._sync_secrets_egress_network_policy("sandbox-pod-3", "session-new", None)

    assert fake_networking.delete_calls[-1]["name"] == secrets_egress_policy_name("sandbox-pod-3")
    assert len(fake_networking.create_calls) == 0
    assert len(fake_networking.replace_calls) == 0


@pytest.mark.asyncio
async def test_sync_swallows_replace_error_without_raising(monkeypatch):
    manager, fake_networking = _manager_with_fake_networking(
        monkeypatch, replace_raises=ApiException(status=500)
    )

    # Must not raise -- fails closed (no egress rule), not the session itself.
    await manager._sync_secrets_egress_network_policy("sandbox-pod-4", "session-abc", SAMPLE_GRANTS)
    assert fake_networking.create_calls == []


@pytest.mark.asyncio
async def test_sync_noop_when_networking_api_unavailable(monkeypatch):
    manager = SandboxManager()
    monkeypatch.setattr("boxkite._manager_tls_auth.BOXKITE_SECRETS_NETWORK_POLICY_ENABLED", True)

    async def fake_init_k8s():
        manager._k8s_networking_api = None

    manager._init_k8s = fake_init_k8s

    # Must not raise even though there's no networking API to call.
    await manager._sync_secrets_egress_network_policy("sandbox-pod-5", "session-abc", SAMPLE_GRANTS)


@pytest.mark.asyncio
async def test_delete_tolerates_already_absent_policy(monkeypatch):
    manager, fake_networking = _manager_with_fake_networking(monkeypatch)

    # No policy was ever created for this pod -- delete must not raise.
    await manager._delete_secrets_egress_network_policy("never-had-one")
    assert fake_networking.delete_calls[-1]["name"] == secrets_egress_policy_name("never-had-one")


@pytest.mark.asyncio
async def test_recycle_pod_via_k8s_deletes_secrets_egress_policy_on_success(monkeypatch):
    """The acceptance criteria's core teardown requirement: a session's
    secrets-egress policy must not survive its pod being recycled back to
    the warm pool for a different tenant to claim next."""
    manager = SandboxManager()
    monkeypatch.setattr("boxkite.manager.WARM_POOL_RECYCLE", True)
    monkeypatch.setattr("boxkite.manager.WARM_POOL_MAX", 100)

    async def fake_init_k8s():
        manager._k8s_core_api = SimpleNamespace(
            list_namespaced_pod=AsyncMock(
                return_value=SimpleNamespace(items=[SimpleNamespace(status=SimpleNamespace(phase="Running"))])
            ),
            patch_namespaced_pod=AsyncMock(),
        )

    class _FakeConfigureResponse:
        def raise_for_status(self):
            return None

    class _FakeHttpxClient:
        def __init__(self, *_a, **_k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_k):
            return _FakeConfigureResponse()

    delete_calls = []

    async def fake_delete_secrets_egress_network_policy(pod_name):
        delete_calls.append(pod_name)

    monkeypatch.setattr(manager, "_kill_all_processes", AsyncMock())
    monkeypatch.setattr(manager, "_init_k8s", fake_init_k8s)
    monkeypatch.setattr(manager, "_auth_headers_for_pod", lambda *_a, **_k: {})
    monkeypatch.setattr(manager, "_ensure_pod_tls_cert_cached", AsyncMock(return_value=""))
    monkeypatch.setattr(
        manager, "_delete_secrets_egress_network_policy", fake_delete_secrets_egress_network_policy
    )
    monkeypatch.setattr("boxkite.manager.httpx.AsyncClient", _FakeHttpxClient)

    recycled = await manager._recycle_pod_via_k8s("sandbox-pod-recycle", "10.8.0.80")

    assert recycled is True
    assert delete_calls == ["sandbox-pod-recycle"]


@pytest.mark.asyncio
async def test_delete_pod_deletes_secrets_egress_policy():
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    delete_calls = []

    async def fake_delete_secrets_egress_network_policy(pod_name):
        delete_calls.append(pod_name)

    manager._delete_secrets_egress_network_policy = fake_delete_secrets_egress_network_policy

    await manager._delete_pod("sandbox-pod-hard-delete")

    assert delete_calls == ["sandbox-pod-hard-delete"]


@pytest.mark.asyncio
async def test_create_k8s_session_syncs_secrets_egress_policy_with_session_label(monkeypatch):
    """End-to-end (mocked K8s) check that _create_k8s_session calls the
    sync method with this session's exact session-id label value and its
    secret_grants, after the pod's identity labels are patched."""
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

    async def fake_sync(pod_name, session_label_value, secret_grants):
        sync_calls.append((pod_name, session_label_value, secret_grants))

    manager._init_k8s = fake_init_k8s
    manager._claim_warm_pod_via_k8s = fake_claim_warm_pod
    manager._create_pod = fake_create_pod
    manager._get_http_client = lambda *_args, **_kwargs: fake_client
    manager._k8s_core_api = SimpleNamespace(patch_namespaced_pod=AsyncMock())
    manager._sync_secrets_egress_network_policy = fake_sync

    session_id = "session-secrets-e2e"
    await manager._create_k8s_session(
        uuid4(),
        session_id,
        None,
        None,
        secret_grants=SAMPLE_GRANTS,
        secret_capability_token="tok",
        secrets_control_plane_url="https://cp.internal",
    )

    assert len(sync_calls) == 1
    pod_name, session_label_value, secret_grants = sync_calls[0]
    assert session_label_value.startswith("session-")
    assert secret_grants == SAMPLE_GRANTS
