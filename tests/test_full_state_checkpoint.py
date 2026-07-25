"""Tests for opt-in full-state (process/memory) checkpoint
(docs/FULL-STATE-SNAPSHOT-SCOPING.md, src/boxkite/checkpoint_backend.py,
src/boxkite/_manager_checkpoint.py).

Forensic-only -- these tests do NOT (and cannot, in this environment)
verify anything against a live cluster with the kubelet's
ContainerCheckpoint feature gate enabled. They cover: the real request
shape sent to the Kubernetes API's node-proxy subresource, response
parsing, restore always being refused, and SandboxManager's own
flag-gating/compose-mode/pod-resolution wiring.
"""

import json
from types import SimpleNamespace

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from boxkite import resource_config
from boxkite.checkpoint_backend import (
    CheckpointRestoreNotSupportedError,
    KubeletForensicCheckpointBackend,
    probe_checkpoint_support,
)
from boxkite.manager import SandboxManager


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(resource_config.BOXKITE_FULL_STATE_SNAPSHOT_ENABLED_ENV, raising=False)


class _FakeNodeProxyCoreApi:
    def __init__(self, *, post_response=None, post_raises=None, get_raises=None):
        self._post_response = post_response
        self._post_raises = post_raises
        self._get_raises = get_raises
        self.post_calls = []
        self.get_calls = []

    async def connect_post_node_proxy_with_path(self, *, name, path):
        self.post_calls.append({"name": name, "path": path})
        if self._post_raises is not None:
            raise self._post_raises
        return self._post_response

    async def connect_get_node_proxy_with_path(self, *, name, path):
        self.get_calls.append({"name": name, "path": path})
        if self._get_raises is not None:
            raise self._get_raises
        return "{}"


# ── KubeletForensicCheckpointBackend ─────────────────────────────────────


async def test_checkpoint_sends_the_kep_2008_documented_proxy_path():
    fake = _FakeNodeProxyCoreApi(post_response=json.dumps({"items": ["/var/lib/kubelet/checkpoints/x.tar"]}))
    backend = KubeletForensicCheckpointBackend(fake)

    result = await backend.checkpoint(
        node_name="node-1", namespace="default", pod_name="sandbox-abc", container_name="sandbox"
    )

    assert fake.post_calls == [{"name": "node-1", "path": "/checkpoint/default/sandbox-abc/sandbox"}]
    assert result.archive_paths == ["/var/lib/kubelet/checkpoints/x.tar"]
    assert result.node_name == "node-1"


async def test_checkpoint_handles_unexpected_response_shape_defensively():
    fake = _FakeNodeProxyCoreApi(post_response="not json at all")
    backend = KubeletForensicCheckpointBackend(fake)

    result = await backend.checkpoint(
        node_name="node-1", namespace="default", pod_name="sandbox-abc", container_name="sandbox"
    )
    assert result.archive_paths == []


async def test_checkpoint_reraises_api_exception_verbatim():
    """Fail loud, not silently -- a 404/501 here almost always means the
    feature gate or CRI support isn't actually enabled on the node, and
    the caller should see the kubelet's own error, not a swallowed one."""
    fake = _FakeNodeProxyCoreApi(post_raises=ApiException(status=404, reason="checkpoint not supported"))
    backend = KubeletForensicCheckpointBackend(fake)

    with pytest.raises(ApiException):
        await backend.checkpoint(
            node_name="node-1", namespace="default", pod_name="sandbox-abc", container_name="sandbox"
        )


async def test_restore_always_raises_not_supported():
    backend = KubeletForensicCheckpointBackend(_FakeNodeProxyCoreApi())
    with pytest.raises(CheckpointRestoreNotSupportedError):
        await backend.restore()


# ── probe_checkpoint_support ─────────────────────────────────────────────


async def test_probe_treats_404_as_available():
    fake = _FakeNodeProxyCoreApi()

    async def _get(*, name, path):
        raise ApiException(status=404)

    fake.connect_get_node_proxy_with_path = _get
    assert await probe_checkpoint_support(fake, "node-1") is True


async def test_probe_treats_403_as_unavailable():
    fake = _FakeNodeProxyCoreApi()

    async def _get(*, name, path):
        raise ApiException(status=403)

    fake.connect_get_node_proxy_with_path = _get
    assert await probe_checkpoint_support(fake, "node-1") is False


# ── SandboxManager.create_full_state_checkpoint ─────────────────────────


async def test_create_full_state_checkpoint_rejected_when_flag_disabled():
    manager = SandboxManager()
    with pytest.raises(RuntimeError, match="BOXKITE_FULL_STATE_SNAPSHOT_ENABLED"):
        await manager.create_full_state_checkpoint("session-1")


async def test_create_full_state_checkpoint_rejected_in_compose_mode(monkeypatch):
    monkeypatch.setenv(resource_config.BOXKITE_FULL_STATE_SNAPSHOT_ENABLED_ENV, "true")
    manager = SandboxManager()
    manager._use_docker_compose = True
    with pytest.raises(RuntimeError, match="Docker Compose"):
        await manager.create_full_state_checkpoint("session-1")


async def test_create_full_state_checkpoint_resolves_pod_and_calls_backend(monkeypatch):
    monkeypatch.setenv(resource_config.BOXKITE_FULL_STATE_SNAPSHOT_ENABLED_ENV, "true")
    manager = SandboxManager()
    manager._use_docker_compose = False

    async def fake_init_k8s():
        pass

    async def fake_resolve_session(session_id):
        return ("sandbox-abc", "10.0.0.5")

    fake_pod = SimpleNamespace(spec=SimpleNamespace(node_name="node-42"))
    fake_core_api = _FakeNodeProxyCoreApi(post_response=json.dumps({"items": ["/tmp/checkpoint.tar"]}))

    async def fake_read_namespaced_pod(*, name, namespace):
        return fake_pod

    fake_core_api.read_namespaced_pod = fake_read_namespaced_pod

    monkeypatch.setattr(manager, "_init_k8s", fake_init_k8s)
    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)
    manager._k8s_core_api = fake_core_api

    result = await manager.create_full_state_checkpoint("session-1", container_name="sandbox")

    assert result.node_name == "node-42"
    assert result.archive_paths == ["/tmp/checkpoint.tar"]
    assert fake_core_api.post_calls == [
        {"name": "node-42", "path": "/checkpoint/default/sandbox-abc/sandbox"}
    ]
