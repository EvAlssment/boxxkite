import asyncio
import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from boxkite.aws_identity import (
    AKS_SKIP_CONTAINERS_ANNOTATION,
    EKS_SKIP_CONTAINERS_ANNOTATION,
    AWS_WEB_IDENTITY_VOLUME_NAME,
)
from boxkite.manager import (
    ORGANIZATION_ID_ANNOTATION,
    SANDBOX_NAMESPACE,
    SESSION_ID_ANNOTATION,
    WORK_ITEM_ID_ANNOTATION,
    SandboxManager,
    _get_s3_bucket,
)
from boxkite.resource_config import (
    SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED_ENV,
    SANDBOX_CONTAINER_CPU_LIMIT_ENV,
    SANDBOX_CONTAINER_CPU_REQUEST_ENV,
    SANDBOX_CONTAINER_MEMORY_LIMIT_ENV,
    SANDBOX_CONTAINER_MEMORY_REQUEST_ENV,
    SANDBOX_SIDECAR_CPU_LIMIT_ENV,
    SANDBOX_SIDECAR_CPU_REQUEST_ENV,
    SANDBOX_SIDECAR_MEMORY_LIMIT_ENV,
    SANDBOX_SIDECAR_MEMORY_REQUEST_ENV,
    build_sandbox_container_resources,
    build_sidecar_exec_network_isolation_env,
    build_sidecar_container_resources,
)
import boxkite.warm_pool as warm_pool_module
from boxkite.warm_pool import WarmPoolManager
from boxkite.warm_pool import _get_s3_bucket as _get_warm_pool_s3_bucket
from boxkite.sidecar_auth import (
    SIDECAR_AUTH_HEADER,
    SIDECAR_AUTH_SECRET_KEY,
    SIDECAR_AUTH_TOKEN_ENV,
    sidecar_auth_secret_name,
)
from boxkite.tls import (
    SIDECAR_TLS_CERT_SECRET_KEY,
    SIDECAR_TLS_DISABLED_ENV,
    SIDECAR_TLS_KEY_SECRET_KEY,
)


pytestmark = pytest.mark.pr


def test_sandbox_s3_bucket_prefers_canonical_env(monkeypatch):
    monkeypatch.setenv("STORAGE_S3_BUCKET", "canonical-bucket")
    monkeypatch.setenv("S3_BUCKET", "legacy-bucket")

    assert _get_s3_bucket() == "canonical-bucket"


def test_sandbox_s3_bucket_falls_back_to_legacy_env(monkeypatch):
    monkeypatch.delenv("STORAGE_S3_BUCKET", raising=False)
    monkeypatch.setenv("S3_BUCKET", "legacy-bucket")

    assert _get_s3_bucket() == "legacy-bucket"


def test_warm_pool_s3_bucket_prefers_canonical_env(monkeypatch):
    monkeypatch.setenv("STORAGE_S3_BUCKET", "canonical-bucket")
    monkeypatch.setenv("S3_BUCKET", "legacy-bucket")

    assert _get_warm_pool_s3_bucket() == "canonical-bucket"


class _FakeCoreApi:
    def __init__(self, list_responses=None, read_responses=None, secrets=None):
        self._list_responses = list(list_responses or [])
        self._read_responses = list(read_responses or [])
        # In-memory secret store, keyed by name -- unlike the pod
        # list/read queues above (pre-scripted per-test scenarios), secrets
        # in these tests are created BY the code under test and then read
        # back, so a stateful fake fits better than a canned-response queue.
        self._secrets: dict[str, SimpleNamespace] = dict(secrets or {})
        self.list_calls = []
        self.read_calls = []
        self.create_calls = []
        self.patch_calls = []
        self.delete_calls = []
        self.secret_create_calls = []
        self.secret_read_calls = []
        self.secret_delete_calls = []

    async def list_namespaced_pod(self, **kwargs):
        self.list_calls.append(kwargs)
        response = self._list_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def read_namespaced_pod(self, **kwargs):
        self.read_calls.append(kwargs)
        response = self._read_responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    async def create_namespaced_pod(self, **kwargs):
        self.create_calls.append(kwargs)
        return None

    async def patch_namespaced_pod(self, **kwargs):
        self.patch_calls.append(kwargs)
        return None

    async def delete_namespaced_pod(self, **kwargs):
        self.delete_calls.append(kwargs)
        return None

    async def create_namespaced_secret(self, **kwargs):
        self.secret_create_calls.append(kwargs)
        body = kwargs["body"]
        name = body.metadata.name
        if name in self._secrets:
            raise ApiException(status=409)
        # Mirror what a real API server does: string_data written on create
        # comes back as base64-encoded `data` on read -- exercises the same
        # base64.b64decode() path _decode_sidecar_auth_token_from_secret uses.
        encoded_data = {
            key: base64.b64encode(value.encode("utf-8")).decode("ascii")
            for key, value in (body.string_data or {}).items()
        }
        stored = SimpleNamespace(metadata=body.metadata, data=encoded_data)
        self._secrets[name] = stored
        return stored

    async def read_namespaced_secret(self, **kwargs):
        self.secret_read_calls.append(kwargs)
        name = kwargs["name"]
        if name not in self._secrets:
            raise ApiException(status=404)
        return self._secrets[name]

    async def delete_namespaced_secret(self, **kwargs):
        self.secret_delete_calls.append(kwargs)
        name = kwargs["name"]
        if name not in self._secrets:
            raise ApiException(status=404)
        del self._secrets[name]
        return None


class _FakeApiClient:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _make_pod(
    *,
    session_id: str,
    pod_name: str = "sandbox-pod",
    pod_ip: str = "10.8.0.80",
):
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=pod_name,
            labels={
                "session-id": session_id,
                "sandbox.boxkite.dev/status": "claimed",
            },
            annotations={SESSION_ID_ANNOTATION: session_id},
        ),
        status=SimpleNamespace(phase="Running", pod_ip=pod_ip),
    )


def _sidecar_env_by_name(pod):
    sidecar = next(container for container in pod.spec.containers if container.name == "sidecar")
    return {env.name: env for env in sidecar.env}


def _decode_test_secret(fake_core_api, secret_name: str) -> str:
    """Decode a sidecar-auth token out of _FakeCoreApi's in-memory secret
    store, mirroring _decode_sidecar_auth_token_from_secret's real logic."""
    secret = fake_core_api._secrets[secret_name]
    return base64.b64decode(secret.data[SIDECAR_AUTH_SECRET_KEY]).decode("utf-8")


def _container_by_name(pod, name):
    return next(container for container in pod.spec.containers if container.name == name)


def _volume_mount_names(container):
    return {mount.name for mount in container.volume_mounts}


def _secret_ref_key(env_var):
    return env_var.value_from.secret_key_ref.key


SANDBOX_RESOURCE_ENV_VARS = (
    SANDBOX_CONTAINER_CPU_REQUEST_ENV,
    SANDBOX_CONTAINER_MEMORY_REQUEST_ENV,
    SANDBOX_CONTAINER_CPU_LIMIT_ENV,
    SANDBOX_CONTAINER_MEMORY_LIMIT_ENV,
    SANDBOX_SIDECAR_CPU_REQUEST_ENV,
    SANDBOX_SIDECAR_MEMORY_REQUEST_ENV,
    SANDBOX_SIDECAR_CPU_LIMIT_ENV,
    SANDBOX_SIDECAR_MEMORY_LIMIT_ENV,
)


def _clear_sandbox_resource_env(monkeypatch):
    for env_name in SANDBOX_RESOURCE_ENV_VARS:
        monkeypatch.delenv(env_name, raising=False)


def _set_custom_sandbox_secret_key_env(monkeypatch):
    monkeypatch.delenv("SANDBOX_AWS_WEB_IDENTITY_ENABLED", raising=False)
    monkeypatch.setenv("AWS_ACCESS_KEY_ID_SECRET_KEY", "custom-aws-access-key-id")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY_SECRET_KEY", "custom-aws-secret-access-key")
    monkeypatch.setenv("STORAGE_S3_ACCESS_KEY_SECRET_KEY", "custom-storage-access-key")
    monkeypatch.setenv("STORAGE_S3_SECRET_KEY_SECRET_KEY", "custom-storage-secret-key")
    monkeypatch.setenv("STORAGE_AZURE_CONNECTION_STRING_SECRET_KEY", "custom-azure-conn")


def test_sandbox_resource_defaults_are_quota_friendly(monkeypatch):
    _clear_sandbox_resource_env(monkeypatch)

    sandbox_resources = build_sandbox_container_resources()
    sidecar_resources = build_sidecar_container_resources()

    assert sandbox_resources.requests == {"cpu": "25m", "memory": "64Mi"}
    assert sandbox_resources.limits == {"cpu": "250m", "memory": "256Mi"}
    assert sidecar_resources.requests == {"cpu": "100m", "memory": "256Mi"}
    assert sidecar_resources.limits == {"cpu": "1000m", "memory": "1Gi"}


def test_medium_and_large_provide_gb_scale_enforced_memory(monkeypatch):
    """The enforced per-sandbox memory budget is the SIDECAR container limit
    (agent code runs charged to the sidecar cgroup, not the sandbox cgroup --
    see resource_config's module note). medium/large must therefore expose
    GB-scale *sidecar* memory limits, scaling up with size."""
    _clear_sandbox_resource_env(monkeypatch)

    small = build_sidecar_container_resources(size="small")
    medium = build_sidecar_container_resources(size="medium")
    large = build_sidecar_container_resources(size="large")

    assert small.limits["memory"] == "1Gi"
    assert medium.limits["memory"] == "2Gi"
    assert large.limits["memory"] == "4Gi"
    # CPU scales alongside memory.
    assert medium.limits["cpu"] == "2000m"
    assert large.limits["cpu"] == "4000m"


def test_sandbox_resources_use_env_overrides(monkeypatch):
    monkeypatch.setenv(SANDBOX_CONTAINER_CPU_REQUEST_ENV, "150m")
    monkeypatch.setenv(SANDBOX_CONTAINER_MEMORY_REQUEST_ENV, "384Mi")
    monkeypatch.setenv(SANDBOX_CONTAINER_CPU_LIMIT_ENV, "1200m")
    monkeypatch.setenv(SANDBOX_CONTAINER_MEMORY_LIMIT_ENV, "3Gi")
    monkeypatch.setenv(SANDBOX_SIDECAR_CPU_REQUEST_ENV, "75m")
    monkeypatch.setenv(SANDBOX_SIDECAR_MEMORY_REQUEST_ENV, "96Mi")
    monkeypatch.setenv(SANDBOX_SIDECAR_CPU_LIMIT_ENV, "300m")
    monkeypatch.setenv(SANDBOX_SIDECAR_MEMORY_LIMIT_ENV, "384Mi")

    sandbox_resources = build_sandbox_container_resources()
    sidecar_resources = build_sidecar_container_resources()

    assert sandbox_resources.requests == {"cpu": "150m", "memory": "384Mi"}
    assert sandbox_resources.limits == {"cpu": "1200m", "memory": "3Gi"}
    assert sidecar_resources.requests == {"cpu": "75m", "memory": "96Mi"}
    assert sidecar_resources.limits == {"cpu": "300m", "memory": "384Mi"}


def test_sidecar_exec_network_isolation_env_defaults_enabled(monkeypatch):
    monkeypatch.delenv(SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED_ENV, raising=False)

    env = build_sidecar_exec_network_isolation_env()

    assert env.name == SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED_ENV
    assert env.value == "true"


def test_sidecar_exec_network_isolation_env_can_disable(monkeypatch):
    monkeypatch.setenv(SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED_ENV, "false")

    env = build_sidecar_exec_network_isolation_env()

    assert env.name == SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED_ENV
    assert env.value == "false"


@pytest.mark.asyncio
async def test_sandbox_pod_uses_configured_storage_secret_keys(monkeypatch):
    _set_custom_sandbox_secret_key_env(monkeypatch)
    monkeypatch.setenv(SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED_ENV, "false")
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.90"

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager._create_pod(
        pod_name="sandbox-custom-secrets",
        session_id="session-custom-secrets",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )

    pod = manager._k8s_core_api.create_calls[0]["body"]
    env = _sidecar_env_by_name(pod)

    assert _secret_ref_key(env["AWS_ACCESS_KEY_ID"]) == "custom-storage-access-key"
    assert _secret_ref_key(env["AWS_SECRET_ACCESS_KEY"]) == "custom-storage-secret-key"
    assert _secret_ref_key(env["STORAGE_AZURE_CONNECTION_STRING"]) == "custom-azure-conn"
    assert env[SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED_ENV].value == "false"


@pytest.mark.asyncio
async def test_sandbox_pod_uses_configured_resources(monkeypatch):
    monkeypatch.setenv(SANDBOX_CONTAINER_CPU_REQUEST_ENV, "150m")
    monkeypatch.setenv(SANDBOX_CONTAINER_MEMORY_REQUEST_ENV, "384Mi")
    monkeypatch.setenv(SANDBOX_CONTAINER_CPU_LIMIT_ENV, "1200m")
    monkeypatch.setenv(SANDBOX_CONTAINER_MEMORY_LIMIT_ENV, "3Gi")
    monkeypatch.setenv(SANDBOX_SIDECAR_CPU_REQUEST_ENV, "75m")
    monkeypatch.setenv(SANDBOX_SIDECAR_MEMORY_REQUEST_ENV, "96Mi")
    monkeypatch.setenv(SANDBOX_SIDECAR_CPU_LIMIT_ENV, "300m")
    monkeypatch.setenv(SANDBOX_SIDECAR_MEMORY_LIMIT_ENV, "384Mi")
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.90"

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager._create_pod(
        pod_name="sandbox-custom-resources",
        session_id="session-custom-resources",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )

    pod = manager._k8s_core_api.create_calls[0]["body"]
    sandbox = _container_by_name(pod, "sandbox")
    sidecar = _container_by_name(pod, "sidecar")

    assert sandbox.resources.requests == {"cpu": "150m", "memory": "384Mi"}
    assert sandbox.resources.limits == {"cpu": "1200m", "memory": "3Gi"}
    assert sidecar.resources.requests == {"cpu": "75m", "memory": "96Mi"}
    assert sidecar.resources.limits == {"cpu": "300m", "memory": "384Mi"}


@pytest.mark.asyncio
async def test_warm_pool_pod_uses_configured_storage_secret_keys(monkeypatch):
    _set_custom_sandbox_secret_key_env(monkeypatch)
    monkeypatch.setenv(SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED_ENV, "false")
    manager = WarmPoolManager()
    manager._k8s_core_api = _FakeCoreApi()

    await manager._create_k8s_pod("sandbox-warm-custom-secrets")

    pod = manager._k8s_core_api.create_calls[0]["body"]
    env = _sidecar_env_by_name(pod)

    assert _secret_ref_key(env["AWS_ACCESS_KEY_ID"]) == "custom-storage-access-key"
    assert _secret_ref_key(env["AWS_SECRET_ACCESS_KEY"]) == "custom-storage-secret-key"
    assert _secret_ref_key(env["STORAGE_AZURE_CONNECTION_STRING"]) == "custom-azure-conn"
    assert env[SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED_ENV].value == "false"


@pytest.mark.asyncio
async def test_warm_pool_pod_uses_configured_resources(monkeypatch):
    monkeypatch.setenv(SANDBOX_CONTAINER_CPU_REQUEST_ENV, "150m")
    monkeypatch.setenv(SANDBOX_CONTAINER_MEMORY_REQUEST_ENV, "384Mi")
    monkeypatch.setenv(SANDBOX_CONTAINER_CPU_LIMIT_ENV, "1200m")
    monkeypatch.setenv(SANDBOX_CONTAINER_MEMORY_LIMIT_ENV, "3Gi")
    monkeypatch.setenv(SANDBOX_SIDECAR_CPU_REQUEST_ENV, "75m")
    monkeypatch.setenv(SANDBOX_SIDECAR_MEMORY_REQUEST_ENV, "96Mi")
    monkeypatch.setenv(SANDBOX_SIDECAR_CPU_LIMIT_ENV, "300m")
    monkeypatch.setenv(SANDBOX_SIDECAR_MEMORY_LIMIT_ENV, "384Mi")
    manager = WarmPoolManager()
    manager._k8s_core_api = _FakeCoreApi()

    await manager._create_k8s_pod("sandbox-warm-custom-resources")

    pod = manager._k8s_core_api.create_calls[0]["body"]
    sandbox = _container_by_name(pod, "sandbox")
    sidecar = _container_by_name(pod, "sidecar")

    assert sandbox.resources.requests == {"cpu": "150m", "memory": "384Mi"}
    assert sandbox.resources.limits == {"cpu": "1200m", "memory": "3Gi"}
    assert sidecar.resources.requests == {"cpu": "75m", "memory": "96Mi"}
    assert sidecar.resources.limits == {"cpu": "300m", "memory": "384Mi"}


@pytest.mark.asyncio
async def test_schedule_replenish_runs_once_and_dedupes():
    """Request-driven replenish (fix for warm-pool starvation under a
    CPU-throttling serverless runtime): a single in-flight top-up runs, and
    concurrent nudges don't stack overlapping scans."""
    manager = WarmPoolManager()
    manager._k8s_core_api = _FakeCoreApi()
    manager._running = True

    calls = {"n": 0}
    release = asyncio.Event()

    async def fake_replenish():
        calls["n"] += 1
        await release.wait()

    manager._replenish = fake_replenish  # type: ignore[assignment]

    manager.schedule_replenish()
    manager.schedule_replenish()  # deduped while the first is still in flight
    await asyncio.sleep(0)
    assert calls["n"] == 1

    release.set()
    await manager._manual_replenish_task
    assert calls["n"] == 1

    # A fresh nudge after the previous one finished runs again.
    release2 = asyncio.Event()

    async def fake_replenish_2():
        calls["n"] += 1
        await release2.wait()

    manager._replenish = fake_replenish_2  # type: ignore[assignment]
    manager.schedule_replenish()
    await asyncio.sleep(0)
    assert calls["n"] == 2
    release2.set()
    await manager._manual_replenish_task


@pytest.mark.asyncio
async def test_schedule_replenish_noop_when_not_running():
    manager = WarmPoolManager()
    manager._k8s_core_api = _FakeCoreApi()
    manager._running = False
    manager.schedule_replenish()
    assert manager._manual_replenish_task is None


@pytest.mark.asyncio
async def test_sandbox_pod_web_identity_skips_provider_webhook_injection(monkeypatch):
    monkeypatch.setenv("SANDBOX_AWS_WEB_IDENTITY_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/shared-sandbox")
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.90"

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager._create_pod(
        pod_name="sandbox-web-identity",
        session_id="session-web-identity",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )

    pod = manager._k8s_core_api.create_calls[0]["body"]
    sandbox = _container_by_name(pod, "sandbox")
    sidecar = _container_by_name(pod, "sidecar")

    assert pod.spec.automount_service_account_token is False
    assert pod.metadata.annotations[EKS_SKIP_CONTAINERS_ANNOTATION] == "sandbox,sidecar"
    assert pod.metadata.annotations[AKS_SKIP_CONTAINERS_ANNOTATION] == "sandbox;sidecar"
    assert AWS_WEB_IDENTITY_VOLUME_NAME not in _volume_mount_names(sandbox)
    assert AWS_WEB_IDENTITY_VOLUME_NAME in _volume_mount_names(sidecar)


@pytest.mark.asyncio
async def test_warm_pool_pod_web_identity_skips_provider_webhook_injection(monkeypatch):
    monkeypatch.setenv("SANDBOX_AWS_WEB_IDENTITY_ENABLED", "true")
    monkeypatch.setenv("SANDBOX_AWS_ROLE_ARN", "arn:aws:iam::123456789012:role/shared-sandbox")
    manager = WarmPoolManager()
    manager._k8s_core_api = _FakeCoreApi()

    await manager._create_k8s_pod("sandbox-warm-web-identity")

    pod = manager._k8s_core_api.create_calls[0]["body"]
    sandbox = _container_by_name(pod, "sandbox")
    sidecar = _container_by_name(pod, "sidecar")

    assert pod.spec.automount_service_account_token is False
    assert pod.metadata.annotations[EKS_SKIP_CONTAINERS_ANNOTATION] == "sandbox,sidecar"
    assert pod.metadata.annotations[AKS_SKIP_CONTAINERS_ANNOTATION] == "sandbox;sidecar"
    assert AWS_WEB_IDENTITY_VOLUME_NAME not in _volume_mount_names(sandbox)
    assert AWS_WEB_IDENTITY_VOLUME_NAME in _volume_mount_names(sidecar)


@pytest.mark.asyncio
async def test_resolve_session_retries_retryable_k8s_transport_error(monkeypatch):
    # Exercise the observable recovery path: a transient transport failure on the
    # first K8s lookup should still return the running sandbox on retry without
    # tearing down the shared singleton client.
    manager = SandboxManager()
    manager._k8s_initialized = True

    first_core_api = _FakeCoreApi(
        list_responses=[ConnectionAbortedError("SSL handshake is taking longer than 60.0 seconds")]
    )
    retry_api_client = _FakeApiClient()
    second_core_api = _FakeCoreApi(
        list_responses=[SimpleNamespace(items=[_make_pod(session_id="session-1")])]
    )
    manager._k8s_core_api = first_core_api

    retry_client_calls = 0

    async def fake_create_retry_k8s_core_api():
        nonlocal retry_client_calls
        retry_client_calls += 1
        return retry_api_client, second_core_api

    monkeypatch.setattr(manager, "_create_retry_k8s_core_api", fake_create_retry_k8s_core_api)

    pod_name, pod_ip = await manager._resolve_session("session-1")

    assert (pod_name, pod_ip) == ("sandbox-pod", "10.8.0.80")
    assert retry_client_calls == 1
    assert retry_api_client.closed is True
    assert manager._k8s_core_api is first_core_api
    assert first_core_api.list_calls == [
        {
            "namespace": SANDBOX_NAMESPACE,
            "label_selector": (
                "app=sandbox,"
                "sandbox.boxkite.dev/status=claimed,"
                "session-id=session-1"
            ),
        }
    ]
    assert second_core_api.list_calls == [
        {
            "namespace": SANDBOX_NAMESPACE,
            "label_selector": (
                "app=sandbox,"
                "sandbox.boxkite.dev/status=claimed,"
                "session-id=session-1"
            ),
        }
    ]


@pytest.mark.asyncio
async def test_resolve_session_uses_cached_endpoint_until_ttl_expires():
    manager = SandboxManager()
    manager._k8s_initialized = True
    core_api = _FakeCoreApi(
        list_responses=[SimpleNamespace(items=[_make_pod(session_id="session-1")])],
        read_responses=[_make_pod(session_id="session-1")],
    )
    manager._k8s_core_api = core_api

    first = await manager._resolve_session("session-1")
    second = await manager._resolve_session("session-1")

    assert first == ("sandbox-pod", "10.8.0.80")
    assert second == first
    assert len(core_api.list_calls) == 1
    assert core_api.read_calls == [
        {
            "name": "sandbox-pod",
            "namespace": SANDBOX_NAMESPACE,
        }
    ]


@pytest.mark.asyncio
async def test_resolve_session_invalidates_cached_endpoint_when_pod_ownership_changes():
    manager = SandboxManager()
    manager._k8s_initialized = True
    manager._cache_session_endpoint("session-1", "sandbox-pod", "10.8.0.80")
    core_api = _FakeCoreApi(
        list_responses=[
            SimpleNamespace(
                items=[_make_pod(session_id="session-1", pod_name="fresh-pod", pod_ip="10.8.0.81")]
            )
        ],
        read_responses=[_make_pod(session_id="session-2", pod_name="sandbox-pod")],
    )
    manager._k8s_core_api = core_api

    pod_name, pod_ip = await manager._resolve_session("session-1")

    assert (pod_name, pod_ip) == ("fresh-pod", "10.8.0.81")
    assert core_api.read_calls == [
        {
            "name": "sandbox-pod",
            "namespace": SANDBOX_NAMESPACE,
        }
    ]
    assert len(core_api.list_calls) == 1
    assert manager._session_endpoints["session-1"][1:] == ("fresh-pod", "10.8.0.81")


@pytest.mark.asyncio
async def test_create_session_serializes_concurrent_same_session_calls(monkeypatch):
    # Two callers racing on the same session should converge on a single pod create
    # and both receive the same reused session result.
    manager = SandboxManager()
    organization_id = uuid4()
    work_item_id = uuid4()
    session_id = "session-1"

    session_ready = False
    create_calls = 0
    create_started = asyncio.Event()
    allow_create_to_finish = asyncio.Event()

    async def fake_resolve_session(target_session_id: str):
        assert target_session_id == session_id
        if session_ready:
            return ("sandbox-pod", "10.8.0.80")
        raise ValueError(f"No running pod found for session {target_session_id}")

    async def fake_create_k8s_session(
        org_id,
        target_session_id: str,
        target_work_item_id,
        upload_file_ids=None,
        **_kwargs,
    ):
        nonlocal create_calls, session_ready
        assert org_id == organization_id
        assert target_session_id == session_id
        assert target_work_item_id == work_item_id
        assert upload_file_ids is None
        create_calls += 1
        create_started.set()
        await allow_create_to_finish.wait()
        session_ready = True
        return {"pod_name": "sandbox-pod"}

    async def fake_prefetch_uploads_for_session(**_kwargs):
        return None

    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)
    monkeypatch.setattr(manager, "_create_k8s_session", fake_create_k8s_session)
    monkeypatch.setattr(manager, "_prefetch_uploads_for_session", fake_prefetch_uploads_for_session)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_args, **_kwargs: object())

    first_task = asyncio.create_task(
        manager.create_session(
            organization_id=organization_id,
            session_id=session_id,
            work_item_id=work_item_id,
        )
    )
    await create_started.wait()

    second_task = asyncio.create_task(
        manager.create_session(
            organization_id=organization_id,
            session_id=session_id,
            work_item_id=work_item_id,
        )
    )

    await asyncio.sleep(0)
    assert create_calls == 1

    allow_create_to_finish.set()
    first_result, second_result = await asyncio.gather(first_task, second_task)

    assert create_calls == 1
    assert first_result == {"pod_name": "sandbox-pod"}
    assert second_result == {"pod_name": "sandbox-pod"}


@pytest.mark.asyncio
async def test_recovery_lock_retained_while_waiters_are_queued():
    manager = SandboxManager()
    session_id = "session-1"
    lock = manager._get_recovery_lock(session_id)

    await lock.acquire()
    waiter_task = asyncio.create_task(lock.acquire())
    await asyncio.sleep(0)

    lock.release()
    manager._release_recovery_lock_if_idle(session_id)

    assert manager._recovery_locks[session_id] is lock

    await waiter_task
    lock.release()
    manager._release_recovery_lock_if_idle(session_id)

    assert session_id not in manager._recovery_locks


@pytest.mark.asyncio
async def test_destroy_session_invalidates_cached_endpoint(monkeypatch):
    manager = SandboxManager()
    manager._use_docker_compose = True
    manager._compose_sessions["session-1"] = {"organization_id": uuid4()}
    manager._cache_session_endpoint("session-1", "sandbox-pod", "10.8.0.80")

    fake_client = SimpleNamespace(post=AsyncMock(return_value=SimpleNamespace(raise_for_status=lambda: None)))
    monkeypatch.setattr(manager, "_get_http_client", lambda *_args, **_kwargs: fake_client)

    await manager.destroy_session("session-1")

    assert "session-1" not in manager._session_endpoints
    # destroy_session now also kills tracked background processes before
    # flushing -- see _kill_all_processes()'s docstring.
    fake_client.post.assert_any_await("/process/kill-all")
    fake_client.post.assert_any_await("/flush")


@pytest.mark.asyncio
async def test_recover_session_invalidates_cached_endpoint_before_recreate(monkeypatch):
    manager = SandboxManager()
    org_id = uuid4()
    work_item_id = uuid4()
    session_id = "session-1"
    manager._cache_session_endpoint(session_id, "stale-pod", "10.8.0.80")

    async def fake_get_session_metadata(target_session_id: str):
        assert target_session_id == session_id
        return {
            "organization_id": org_id,
            "work_item_id": work_item_id,
            "upload_file_ids": ["file-1"],
        }

    recreate = AsyncMock()
    monkeypatch.setattr(manager, "_get_session_metadata", fake_get_session_metadata)
    monkeypatch.setattr(manager, "create_session", recreate)

    await manager._recover_session_after_sidecar_error(session_id, RuntimeError("sidecar down"))

    assert session_id not in manager._session_endpoints
    recreate.assert_awaited_once_with(
        organization_id=org_id,
        session_id=session_id,
        work_item_id=work_item_id,
        upload_file_ids=["file-1"],
    )


@pytest.mark.asyncio
async def test_recover_session_restores_cached_skills_when_recreate_clears_cache(monkeypatch):
    manager = SandboxManager()
    org_id = uuid4()
    work_item_id = uuid4()
    session_id = "session-1"
    skills = [{"instance_slug": "document/pptx", "files": []}]
    manager._cache_session_skills(session_id, skills)

    async def fake_get_session_metadata(target_session_id: str):
        assert target_session_id == session_id
        return {
            "organization_id": org_id,
            "work_item_id": work_item_id,
            "upload_file_ids": [],
        }

    async def fake_create_session(**_kwargs):
        manager._session_skills.pop(session_id, None)
        return {"pod_name": "recovered-pod"}

    monkeypatch.setattr(manager, "_get_session_metadata", fake_get_session_metadata)
    monkeypatch.setattr(manager, "create_session", fake_create_session)

    await manager._recover_session_after_sidecar_error(
        session_id,
        RuntimeError("sidecar down"),
    )

    assert manager._session_skills[session_id] == skills


@pytest.mark.asyncio
async def test_create_session_preserves_cached_skills_during_internal_recreate(monkeypatch):
    manager = SandboxManager()
    org_id = uuid4()
    work_item_id = uuid4()
    session_id = "session-1"
    skills = [{"instance_slug": "document/pptx", "files": []}]
    manager._cache_session_skills(session_id, skills)

    async def fake_resolve_session(target_session_id: str):
        assert target_session_id == session_id
        return ("stale-pod", "10.8.0.80")

    async def fake_prefetch_uploads_for_session(**_kwargs):
        raise httpx.RemoteProtocolError("sidecar disconnected")

    async def fake_destroy_session(target_session_id: str, *, preserve_cached_skills: bool = False):
        assert target_session_id == session_id
        assert preserve_cached_skills is True
        if not preserve_cached_skills:
            manager._session_skills.pop(session_id, None)

    async def fake_create_k8s_session(*_args, **_kwargs):
        return {"pod_name": "fresh-pod"}

    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)
    monkeypatch.setattr(manager, "_prefetch_uploads_for_session", fake_prefetch_uploads_for_session)
    monkeypatch.setattr(manager, "destroy_session", fake_destroy_session)
    monkeypatch.setattr(manager, "_create_k8s_session", fake_create_k8s_session)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_args, **_kwargs: object())

    result = await manager.create_session(
        organization_id=org_id,
        session_id=session_id,
        work_item_id=work_item_id,
    )

    assert result == {"pod_name": "fresh-pod"}
    assert manager._session_skills[session_id] == skills


@pytest.mark.asyncio
async def test_call_sidecar_with_recovery_replays_cached_skills_before_retry(monkeypatch):
    manager = SandboxManager()
    session_id = "session-1"
    skills = [{"instance_slug": "document/pptx", "files": []}]
    manager._cache_session_skills(session_id, skills)
    events = []
    attempts = 0

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"changed": True}

    class FakeHttpClient:
        async def post(self, path, json):
            events.append(("post", path, json["skills"]))
            return FakeResponse()

    async def request_fn():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            events.append("tool-failed")
            raise httpx.RemoteProtocolError("server disconnected")
        events.append("tool-retried")
        return {"ok": True}

    async def fake_session_sidecar_available(target_session_id: str):
        assert target_session_id == session_id
        events.append("availability-check")
        return False

    async def fake_recover_session(target_session_id: str, _error: Exception):
        assert target_session_id == session_id
        events.append("recover")

    async def fake_resolve_session(target_session_id: str):
        assert target_session_id == session_id
        return ("recovered-pod", "10.8.0.81")

    monkeypatch.setattr(manager, "_session_sidecar_available", fake_session_sidecar_available)
    monkeypatch.setattr(manager, "_recover_session_after_sidecar_error", fake_recover_session)
    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_args, **_kwargs: FakeHttpClient())

    result = await manager._call_sidecar_with_recovery(
        session_id=session_id,
        operation="view",
        request_fn=request_fn,
    )

    assert result == {"ok": True}
    assert events == [
        "tool-failed",
        "availability-check",
        "recover",
        ("post", "/ensure-skills", skills),
        "tool-retried",
    ]


@pytest.mark.asyncio
async def test_call_sidecar_with_recovery_replays_cached_skills_after_concurrent_recovery(monkeypatch):
    manager = SandboxManager()
    session_id = "session-1"
    skills = [{"instance_slug": "document/pptx", "files": []}]
    manager._cache_session_skills(session_id, skills)
    events = []
    attempts = 0

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"changed": True}

    class FakeHttpClient:
        async def post(self, path, json):
            events.append(("post", path, json["skills"]))
            return FakeResponse()

    async def request_fn():
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            events.append("tool-failed")
            raise httpx.RemoteProtocolError("server disconnected")
        events.append("tool-retried")
        return {"ok": True}

    async def fake_session_sidecar_available(target_session_id: str):
        assert target_session_id == session_id
        events.append("availability-check")
        return True

    async def fake_recover_session(_target_session_id: str, _error: Exception):
        raise AssertionError("recovery should not run when sidecar is already healthy")

    async def fake_resolve_session(target_session_id: str):
        assert target_session_id == session_id
        return ("recovered-pod", "10.8.0.81")

    monkeypatch.setattr(manager, "_session_sidecar_available", fake_session_sidecar_available)
    monkeypatch.setattr(manager, "_recover_session_after_sidecar_error", fake_recover_session)
    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_args, **_kwargs: FakeHttpClient())

    result = await manager._call_sidecar_with_recovery(
        session_id=session_id,
        operation="view",
        request_fn=request_fn,
    )

    assert result == {"ok": True}
    assert events == [
        "tool-failed",
        "availability-check",
        ("post", "/ensure-skills", skills),
        "tool-retried",
    ]


@pytest.mark.asyncio
async def test_call_sidecar_with_recovery_serializes_concurrent_skill_replays(monkeypatch):
    manager = SandboxManager()
    session_id = "session-1"
    skills = [{"instance_slug": "document/pptx", "files": []}]
    manager._cache_session_skills(session_id, skills)
    availability_checks = 0
    active_replays = 0
    max_active_replays = 0
    replay_call_count = 0
    first_replay_started = asyncio.Event()
    release_first_replay = asyncio.Event()

    class FakeResponse:
        def __init__(self, payload=None):
            self._payload = payload or {"changed": True}

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeHttpClient:
        async def post(self, path, json):
            nonlocal active_replays, max_active_replays, replay_call_count
            assert path == "/ensure-skills"
            assert json["skills"] == skills

            active_replays += 1
            max_active_replays = max(max_active_replays, active_replays)
            replay_call_count += 1
            try:
                if replay_call_count == 1:
                    first_replay_started.set()
                    await release_first_replay.wait()
                else:
                    await asyncio.sleep(0)
                return FakeResponse()
            finally:
                active_replays -= 1

    def make_request_fn():
        attempts = 0

        async def request_fn():
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise httpx.RemoteProtocolError("server disconnected")
            return {"ok": True}

        return request_fn

    async def fake_session_sidecar_available(target_session_id: str):
        nonlocal availability_checks
        assert target_session_id == session_id
        availability_checks += 1
        return availability_checks > 1

    async def fake_recover_session(target_session_id: str, _error: Exception):
        assert target_session_id == session_id

    async def fake_resolve_session(target_session_id: str):
        assert target_session_id == session_id
        return ("recovered-pod", "10.8.0.81")

    monkeypatch.setattr(manager, "_session_sidecar_available", fake_session_sidecar_available)
    monkeypatch.setattr(manager, "_recover_session_after_sidecar_error", fake_recover_session)
    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_args, **_kwargs: FakeHttpClient())

    first_call = asyncio.create_task(
        manager._call_sidecar_with_recovery(
            session_id=session_id,
            operation="view",
            request_fn=make_request_fn(),
        )
    )
    second_call = asyncio.create_task(
        manager._call_sidecar_with_recovery(
            session_id=session_id,
            operation="view",
            request_fn=make_request_fn(),
        )
    )

    await asyncio.wait_for(first_replay_started.wait(), timeout=1)
    await asyncio.sleep(0.05)
    assert max_active_replays == 1

    release_first_replay.set()
    results = await asyncio.gather(first_call, second_call)

    assert results == [{"ok": True}, {"ok": True}]
    assert replay_call_count == 2
    assert max_active_replays == 1


@pytest.mark.asyncio
async def test_ensure_skills_recovery_returns_replay_result(monkeypatch):
    manager = SandboxManager()
    session_id = "session-1"
    skills = [{"instance_slug": "document/pptx", "files": []}]
    post_results = [
        httpx.RemoteProtocolError("server disconnected"),
        {"changed": True, "skills_rev": "recovered"},
        {"changed": False, "skills_rev": "stale-retry"},
    ]
    post_calls = []

    class FakeResponse:
        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    class FakeHttpClient:
        async def post(self, path, json):
            assert path == "/ensure-skills"
            post_calls.append(json)
            result = post_results.pop(0)
            if isinstance(result, Exception):
                raise result
            return FakeResponse(result)

    async def fake_session_sidecar_available(target_session_id: str):
        assert target_session_id == session_id
        return False

    async def fake_recover_session(target_session_id: str, _error: Exception):
        assert target_session_id == session_id

    async def fake_resolve_session(target_session_id: str):
        assert target_session_id == session_id
        return ("recovered-pod", "10.8.0.81")

    monkeypatch.setattr(manager, "_session_sidecar_available", fake_session_sidecar_available)
    monkeypatch.setattr(manager, "_recover_session_after_sidecar_error", fake_recover_session)
    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_args, **_kwargs: FakeHttpClient())

    result = await manager.ensure_skills(session_id=session_id, skills=skills)

    assert result == {"changed": True, "skills_rev": "recovered"}


# =============================================================================
# Sidecar HTTP auth (Critical #1): SandboxManager/WarmPoolManager must
# generate a fresh per-pod secret, inject it into the sidecar container, and
# send it back on every HTTP call.
# =============================================================================


@pytest.mark.asyncio
async def test_sandbox_pod_gets_a_fresh_sidecar_auth_token(monkeypatch):
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.90"

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager._create_pod(
        pod_name="sandbox-auth-token",
        session_id="session-auth-token",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )

    pod = manager._k8s_core_api.create_calls[0]["body"]
    env = _sidecar_env_by_name(pod)

    # No literal value: the token is only readable via the referenced
    # Secret, requiring separate `secrets: get` RBAC -- see sidecar_auth.py.
    assert env[SIDECAR_AUTH_TOKEN_ENV].value is None
    secret_ref = env[SIDECAR_AUTH_TOKEN_ENV].value_from.secret_key_ref
    assert secret_ref.name == sidecar_auth_secret_name("sandbox-auth-token")
    assert secret_ref.key == SIDECAR_AUTH_SECRET_KEY

    token_from_secret = _decode_test_secret(manager._k8s_core_api, secret_ref.name)
    assert token_from_secret  # non-empty
    assert manager._get_pod_auth_token("sandbox-auth-token") == token_from_secret


@pytest.mark.asyncio
async def test_two_sandbox_pods_get_different_sidecar_auth_tokens(monkeypatch):
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.90"

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager._create_pod(
        pod_name="sandbox-a", session_id="session-a",
        organization_id=uuid4(), work_item_id=uuid4(),
    )
    await manager._create_pod(
        pod_name="sandbox-b", session_id="session-b",
        organization_id=uuid4(), work_item_id=uuid4(),
    )

    pod_a = manager._k8s_core_api.create_calls[0]["body"]
    pod_b = manager._k8s_core_api.create_calls[1]["body"]

    secret_a = _sidecar_env_by_name(pod_a)[SIDECAR_AUTH_TOKEN_ENV].value_from.secret_key_ref.name
    secret_b = _sidecar_env_by_name(pod_b)[SIDECAR_AUTH_TOKEN_ENV].value_from.secret_key_ref.name
    assert secret_a != secret_b  # deterministic from distinct pod names

    token_a = _decode_test_secret(manager._k8s_core_api, secret_a)
    token_b = _decode_test_secret(manager._k8s_core_api, secret_b)
    assert token_a != token_b


@pytest.mark.asyncio
async def test_warm_pool_pod_gets_a_fresh_sidecar_auth_token(monkeypatch):
    manager = WarmPoolManager()
    manager._k8s_core_api = _FakeCoreApi()

    await manager._create_k8s_pod("sandbox-warm-auth-token")

    pod = manager._k8s_core_api.create_calls[0]["body"]
    env = _sidecar_env_by_name(pod)

    assert env[SIDECAR_AUTH_TOKEN_ENV].value is None
    secret_ref = env[SIDECAR_AUTH_TOKEN_ENV].value_from.secret_key_ref
    assert secret_ref.name == sidecar_auth_secret_name("sandbox-warm-auth-token")
    assert secret_ref.key == SIDECAR_AUTH_SECRET_KEY

    token_from_secret = _decode_test_secret(manager._k8s_core_api, secret_ref.name)
    assert token_from_secret


@pytest.mark.asyncio
async def test_http_client_sends_sidecar_auth_header_for_created_pod(monkeypatch):
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.90"

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager._create_pod(
        pod_name="sandbox-header-test",
        session_id="session-header-test",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )
    pod = manager._k8s_core_api.create_calls[0]["body"]
    secret_ref = _sidecar_env_by_name(pod)[SIDECAR_AUTH_TOKEN_ENV].value_from.secret_key_ref
    expected_token = _decode_test_secret(manager._k8s_core_api, secret_ref.name)

    http_client = manager._get_http_client("sandbox-header-test", "10.8.0.90")

    assert http_client.headers[SIDECAR_AUTH_HEADER] == expected_token


@pytest.mark.asyncio
async def test_claiming_warm_pod_recovers_its_auth_token_from_secret():
    """
    A warm pod is created by WarmPoolManager (a different process/class) —
    SandboxManager must recover its token from the pod's sidecar-auth Secret
    when it claims it, not require having created it itself. The token no
    longer lives on a pod annotation (readable via mere `pods: get/list`
    RBAC) — see sidecar_auth.py's module docstring.
    """
    manager = SandboxManager()
    pod_name = "sandbox-warm-claimable"
    warm_pod = SimpleNamespace(
        metadata=SimpleNamespace(
            name=pod_name,
            resource_version="1",
            annotations={},
            labels={},
            creation_timestamp=None,
        ),
        status=SimpleNamespace(
            phase="Running",
            pod_ip="10.8.0.95",
            container_statuses=[SimpleNamespace(ready=True)],
        ),
    )
    secret_name = sidecar_auth_secret_name(pod_name)
    encoded_token = base64.b64encode(b"warm-pool-generated-token").decode("ascii")
    manager._k8s_core_api = _FakeCoreApi(
        list_responses=[SimpleNamespace(items=[warm_pod])],
        secrets={secret_name: SimpleNamespace(data={SIDECAR_AUTH_SECRET_KEY: encoded_token})},
    )

    claimed = await manager._claim_warm_pod_via_k8s()

    assert claimed == (pod_name, "10.8.0.95")
    assert manager._get_pod_auth_token(pod_name) == "warm-pool-generated-token"


@pytest.mark.asyncio
async def test_claiming_warm_pod_reads_secret_exactly_once():
    """Regression test for issue #178: claim used to read the sidecar-auth
    Secret twice (once for the token, once for the cert) -- must be one
    read_namespaced_secret call total."""
    manager = SandboxManager()
    pod_name = "sandbox-warm-single-read"
    warm_pod = SimpleNamespace(
        metadata=SimpleNamespace(
            name=pod_name,
            resource_version="1",
            annotations={},
            labels={},
            creation_timestamp=None,
        ),
        status=SimpleNamespace(
            phase="Running",
            pod_ip="10.8.0.96",
            container_statuses=[SimpleNamespace(ready=True)],
        ),
    )
    secret_name = sidecar_auth_secret_name(pod_name)
    manager._k8s_core_api = _FakeCoreApi(
        list_responses=[SimpleNamespace(items=[warm_pod])],
        secrets={
            secret_name: SimpleNamespace(
                data={SIDECAR_AUTH_SECRET_KEY: base64.b64encode(b"tok").decode("ascii")}
            )
        },
    )

    claimed = await manager._claim_warm_pod_via_k8s()

    assert claimed == (pod_name, "10.8.0.96")
    assert len(manager._k8s_core_api.secret_read_calls) == 1


@pytest.mark.asyncio
async def test_ensure_pod_secret_cached_reads_once_when_tls_disabled():
    """Regression test: _cache_pod_tls_cert never stores an empty string, so
    with TLS disabled cluster-wide (no cert in the Secret) a token/cert
    presence check alone can never short-circuit -- every call would re-read
    the Secret. Must short-circuit on _pod_secrets_fetched instead, reading
    the Secret exactly once across repeated calls for the same pod."""
    manager = SandboxManager()
    pod_name = "sandbox-tls-disabled"
    secret_name = sidecar_auth_secret_name(pod_name)
    manager._k8s_core_api = _FakeCoreApi(
        secrets={
            secret_name: SimpleNamespace(
                data={SIDECAR_AUTH_SECRET_KEY: base64.b64encode(b"tok").decode("ascii")}
                # No TLS cert key present -- mirrors a TLS-disabled deployment.
            )
        },
    )

    for _ in range(4):
        token = await manager._ensure_pod_auth_token_cached(pod_name)
        cert = await manager._ensure_pod_tls_cert_cached(pod_name)
        assert token == "tok"
        assert cert == ""

    assert len(manager._k8s_core_api.secret_read_calls) == 1


@pytest.mark.asyncio
async def test_create_pod_409_retry_reads_secret_exactly_once():
    """Regression test for issue #178: the 409-conflict "existing pod is
    already running, reuse it" branch in _create_pod used to call
    _ensure_pod_auth_token_cached and _ensure_pod_tls_cert_cached
    separately -- two secret reads for one Secret. Must use the combined
    _ensure_pod_secret_cached call, one read total.

    Isolates ONLY the pod-creation 409 race (not the sidecar-auth-secret
    409 race _create_sidecar_auth_secret already handles separately at
    manager.py:301-314) -- the Secret is created fresh here (no pre-seeded
    conflict), so the one read counted below comes solely from the
    pod-reuse branch's deliberate cache-pop-then-refetch."""
    manager = SandboxManager()
    pod_name = "sandbox-409-retry"

    class _FakeCoreApi409(_FakeCoreApi):
        async def create_namespaced_pod(self, **kwargs):
            self.create_calls.append(kwargs)
            raise ApiException(status=409)

    manager._k8s_core_api = _FakeCoreApi409(
        read_responses=[
            SimpleNamespace(
                status=SimpleNamespace(phase="Running", pod_ip="10.8.0.97"),
            )
        ],
    )

    pod_ip = await manager._create_pod(
        pod_name=pod_name,
        session_id="s-409",
        organization_id=None,
        work_item_id=None,
    )

    assert pod_ip == "10.8.0.97"
    assert len(manager._k8s_core_api.secret_read_calls) == 1


def test_get_http_client_without_any_known_token_omits_auth_header():
    """No token cached and not compose mode -> no header sent (the sidecar
    will correctly reject the request with 401/503; this just documents that
    the manager doesn't silently invent a value)."""
    manager = SandboxManager()

    http_client = manager._get_http_client("unknown-pod", "10.8.0.99")

    assert SIDECAR_AUTH_HEADER not in http_client.headers


def test_compose_mode_manager_uses_env_configured_token(monkeypatch):
    monkeypatch.setenv(SIDECAR_AUTH_TOKEN_ENV, "compose-secret-from-env")
    monkeypatch.setenv("RUNTIME_MODE", "compose")

    manager = SandboxManager()
    http_client = manager._get_http_client("compose-sandbox", "localhost")

    assert http_client.headers[SIDECAR_AUTH_HEADER] == "compose-secret-from-env"


@pytest.mark.asyncio
async def test_sandbox_manager_delete_pod_also_deletes_sidecar_auth_secret():
    """The companion Secret must be cleaned up whenever the pod is -- an
    orphaned Secret left behind would just be a leaked-but-unused credential,
    but deleting it deterministically (same name every time) is cheap and
    keeps the cluster tidy."""
    manager = SandboxManager()
    pod_name = "sandbox-to-delete"
    secret_name = sidecar_auth_secret_name(pod_name)
    manager._k8s_core_api = _FakeCoreApi(
        secrets={secret_name: SimpleNamespace(data={SIDECAR_AUTH_SECRET_KEY: "irrelevant"})},
    )

    await manager._delete_pod(pod_name)

    assert manager._k8s_core_api.delete_calls[0]["name"] == pod_name
    assert manager._k8s_core_api.secret_delete_calls[0]["name"] == secret_name
    assert secret_name not in manager._k8s_core_api._secrets


@pytest.mark.asyncio
async def test_sandbox_manager_delete_pod_tolerates_missing_secret():
    """No companion Secret ever existed (e.g. compose mode never created
    one, or it was already cleaned up) -- deletion must not raise."""
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    await manager._delete_pod("sandbox-no-secret")  # must not raise


@pytest.mark.asyncio
async def test_warm_pool_delete_pod_also_deletes_sidecar_auth_secret():
    manager = WarmPoolManager()
    pod_name = "sandbox-warm-to-delete"
    secret_name = sidecar_auth_secret_name(pod_name)
    manager._k8s_core_api = _FakeCoreApi(
        secrets={secret_name: SimpleNamespace(data={SIDECAR_AUTH_SECRET_KEY: "irrelevant"})},
    )

    await manager._delete_pod(pod_name)

    assert manager._k8s_core_api.delete_calls[0]["name"] == pod_name
    assert manager._k8s_core_api.secret_delete_calls[0]["name"] == secret_name
    assert secret_name not in manager._k8s_core_api._secrets


class _FakeConfigureResponse:
    def raise_for_status(self):
        pass


class _FakeConfigureClient:
    """Stands in for httpx.AsyncClient in WarmPoolManager.recycle_pod's
    /configure POST -- these tests only care about the K8s patch that
    follows it, not the HTTP call itself."""

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def post(self, *args, **kwargs):
        return _FakeConfigureResponse()


@pytest.mark.asyncio
async def test_recycle_pod_clears_tenant_identity_annotations(monkeypatch):
    """Regression test: recycle_pod (the path the idle reaper uses to return
    a claimed pod to the warm pool) used to clear the session-id/
    organization-id/work-item-id K8s *labels* but never the matching
    *annotations* -- unlike SandboxManager._recycle_pod_via_k8s, which
    clears both. Since session-cache validation (manager.py's
    _metadata_matches_session_id) prefers a non-empty annotation over the
    label, a stale annotation left behind here could make a different
    org's cached session_id -> pod_name mapping validate successfully
    against a pod that's actually being handed to a new tenant."""
    monkeypatch.setattr(warm_pool_module.httpx, "AsyncClient", _FakeConfigureClient)
    manager = WarmPoolManager()
    manager._k8s_core_api = _FakeCoreApi(list_responses=[SimpleNamespace(items=[])])

    result = await manager.recycle_pod("sandbox-warm-recycle", "10.0.0.9")

    assert result is True
    assert len(manager._k8s_core_api.patch_calls) == 1
    annotations = manager._k8s_core_api.patch_calls[0]["body"]["metadata"]["annotations"]
    assert annotations[SESSION_ID_ANNOTATION] is None
    assert annotations[ORGANIZATION_ID_ANNOTATION] is None
    assert annotations[WORK_ITEM_ID_ANNOTATION] is None


@pytest.mark.asyncio
async def test_create_pod_reconciles_token_when_secret_already_existed(monkeypatch):
    """If create_namespaced_secret 409s (a Secret by this deterministic name
    already exists -- e.g. a concurrent create racing on the same computed
    pod name), the locally generated token is NOT necessarily what's
    actually stored. The manager must read back and cache the real value,
    not its own possibly-mismatched local variable."""
    manager = SandboxManager()
    pod_name = "sandbox-race"
    secret_name = sidecar_auth_secret_name(pod_name)
    pre_existing_token = "already-won-this-race"
    manager._k8s_core_api = _FakeCoreApi(
        secrets={
            secret_name: SimpleNamespace(
                data={SIDECAR_AUTH_SECRET_KEY: base64.b64encode(pre_existing_token.encode()).decode("ascii")}
            )
        },
    )

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.90"

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager._create_pod(
        pod_name=pod_name,
        session_id="session-race",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )

    # The secret was never overwritten (create 409'd, not replaced)...
    assert _decode_test_secret(manager._k8s_core_api, secret_name) == pre_existing_token
    # ...and the manager's own cache/env reference the SAME real value, not
    # whatever it locally generated before discovering the 409.
    assert manager._get_pod_auth_token(pod_name) == pre_existing_token
    pod = manager._k8s_core_api.create_calls[0]["body"]
    assert _sidecar_env_by_name(pod)[SIDECAR_AUTH_TOKEN_ENV].value_from.secret_key_ref.name == secret_name


def _stale_warm_pod(pod_name: str) -> SimpleNamespace:
    """A warm pod old enough that _scan_pool_state classifies it as stale
    (past the default ~23h max claimable age)."""
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=pod_name,
            labels={"app": "sandbox", "pool": "warm", "sandbox.boxkite.dev/status": "warm"},
            creation_timestamp=datetime.now(timezone.utc) - timedelta(hours=25),
        ),
        status=SimpleNamespace(
            phase="Running",
            container_statuses=[SimpleNamespace(ready=True)],
        ),
    )


@pytest.mark.asyncio
async def test_replenish_cleans_up_secret_even_when_stale_pod_delete_fails():
    """Regression test: _replenish's stale-pod cleanup used to run
    delete_namespaced_pod and _delete_sidecar_auth_secret under one shared
    try/except -- if the pod delete raised for ANY reason (including a
    benign 404 from racing the idle reaper's own delete of the same pod),
    the secret delete was skipped entirely, permanently orphaning it (no
    other cleanup path ever revisits it). The secret cleanup must run
    regardless of whether the pod delete succeeded."""
    manager = WarmPoolManager()
    pod_name = "sandbox-warm-stale"
    secret_name = sidecar_auth_secret_name(pod_name)
    fake_api = _FakeCoreApi(
        list_responses=[SimpleNamespace(items=[_stale_warm_pod(pod_name)])],
        secrets={secret_name: SimpleNamespace(data={SIDECAR_AUTH_SECRET_KEY: "irrelevant"})},
    )

    async def _failing_delete_pod(**kwargs):
        fake_api.delete_calls.append(kwargs)
        raise ApiException(status=500, reason="simulated non-404 failure")

    fake_api.delete_namespaced_pod = _failing_delete_pod
    manager._k8s_core_api = fake_api

    async def _no_create(*_args, **_kwargs):
        return None

    manager._create_warm_pod = _no_create

    await manager._replenish()

    assert fake_api.delete_calls  # the pod delete was attempted
    assert fake_api.secret_delete_calls[0]["name"] == secret_name
    assert secret_name not in fake_api._secrets


# =============================================================================
# Manager-to-sidecar TLS (docs/SIDECAR-TRANSPORT-TLS-DESIGN.md)
# =============================================================================


def _decode_test_tls_cert(fake_core_api, secret_name: str) -> str:
    secret = fake_core_api._secrets[secret_name]
    return base64.b64decode(secret.data[SIDECAR_TLS_CERT_SECRET_KEY]).decode("utf-8")


@pytest.mark.asyncio
async def test_create_pod_stores_tls_cert_and_key_in_the_same_secret(monkeypatch):
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.90"

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager._create_pod(
        pod_name="sandbox-tls-secret",
        session_id="session-tls-secret",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )

    secret_name = sidecar_auth_secret_name("sandbox-tls-secret")
    secret = manager._k8s_core_api._secrets[secret_name]
    # Same Secret object as the auth token -- not a second Secret.
    assert SIDECAR_AUTH_SECRET_KEY in secret.data
    assert SIDECAR_TLS_CERT_SECRET_KEY in secret.data
    assert SIDECAR_TLS_KEY_SECRET_KEY in secret.data

    cert_pem = _decode_test_tls_cert(manager._k8s_core_api, secret_name)
    assert "BEGIN CERTIFICATE" in cert_pem
    assert manager._get_pod_tls_cert("sandbox-tls-secret") == cert_pem


@pytest.mark.asyncio
async def test_create_pod_mounts_tls_secret_volume_on_sidecar_container(monkeypatch):
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.90"

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager._create_pod(
        pod_name="sandbox-tls-mount",
        session_id="session-tls-mount",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )

    pod = manager._k8s_core_api.create_calls[0]["body"]
    sidecar = _container_by_name(pod, "sidecar")
    assert "sidecar-tls" in _volume_mount_names(sidecar)

    volume = next(v for v in pod.spec.volumes if v.name == "sidecar-tls")
    assert volume.secret.secret_name == sidecar_auth_secret_name("sandbox-tls-mount")

    env = _sidecar_env_by_name(pod)
    assert env[SIDECAR_TLS_DISABLED_ENV].value == ""


@pytest.mark.asyncio
async def test_create_pod_sidecar_readiness_probe_uses_https_scheme(monkeypatch):
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.90"

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager._create_pod(
        pod_name="sandbox-tls-probe",
        session_id="session-tls-probe",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )

    pod = manager._k8s_core_api.create_calls[0]["body"]
    sidecar = _container_by_name(pod, "sidecar")
    assert sidecar.readiness_probe.http_get.scheme == "HTTPS"


@pytest.mark.asyncio
async def test_sidecar_tls_disabled_skips_cert_generation_and_uses_http(monkeypatch):
    monkeypatch.setenv(SIDECAR_TLS_DISABLED_ENV, "true")
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.90"

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager._create_pod(
        pod_name="sandbox-tls-disabled",
        session_id="session-tls-disabled",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )

    secret_name = sidecar_auth_secret_name("sandbox-tls-disabled")
    secret = manager._k8s_core_api._secrets[secret_name]
    assert SIDECAR_TLS_CERT_SECRET_KEY not in secret.data
    assert SIDECAR_TLS_KEY_SECRET_KEY not in secret.data

    pod = manager._k8s_core_api.create_calls[0]["body"]
    sidecar = _container_by_name(pod, "sidecar")
    assert "sidecar-tls" not in _volume_mount_names(sidecar)
    assert sidecar.readiness_probe.http_get.scheme == "HTTP"
    assert _sidecar_env_by_name(pod)[SIDECAR_TLS_DISABLED_ENV].value == "true"

    assert manager._build_sidecar_url("sandbox-tls-disabled", "10.8.0.90") == "http://10.8.0.90:8080"


def test_build_sidecar_url_defaults_to_https(monkeypatch):
    monkeypatch.delenv(SIDECAR_TLS_DISABLED_ENV, raising=False)
    manager = SandboxManager()
    assert manager._build_sidecar_url("sandbox-x", "10.8.0.90") == "https://10.8.0.90:8080"


def test_build_sidecar_url_falls_back_to_http_under_k8s_proxy(monkeypatch):
    monkeypatch.delenv(SIDECAR_TLS_DISABLED_ENV, raising=False)
    monkeypatch.setenv("SANDBOX_USE_K8S_PROXY", "true")
    manager = SandboxManager()
    url = manager._build_sidecar_url("sandbox-x", "10.8.0.90")
    assert url.startswith("http://")
    assert "https://" not in url


@pytest.mark.asyncio
async def test_http_client_verify_is_pinned_ssl_context_for_created_pod(monkeypatch):
    monkeypatch.delenv(SIDECAR_TLS_DISABLED_ENV, raising=False)
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.90"

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager._create_pod(
        pod_name="sandbox-tls-pin",
        session_id="session-tls-pin",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )

    http_client = manager._get_http_client("sandbox-tls-pin", "10.8.0.90")

    import ssl as _ssl

    assert isinstance(http_client._transport._pool._ssl_context, _ssl.SSLContext)
    assert http_client._transport._pool._ssl_context.check_hostname is False


@pytest.mark.asyncio
async def test_http_client_verify_defaults_true_when_tls_disabled(monkeypatch):
    monkeypatch.setenv(SIDECAR_TLS_DISABLED_ENV, "true")
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.8.0.90"

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    await manager._create_pod(
        pod_name="sandbox-tls-off",
        session_id="session-tls-off",
        organization_id=uuid4(),
        work_item_id=uuid4(),
    )

    # Should not raise even though no cert exists for this pod.
    http_client = manager._get_http_client("sandbox-tls-off", "10.8.0.90")
    assert str(http_client.base_url).startswith("http://")


@pytest.mark.asyncio
async def test_claiming_warm_pod_recovers_its_tls_cert_from_secret():
    """Mirrors test_claiming_warm_pod_recovers_its_auth_token_from_secret --
    a warm pod's TLS cert (created by WarmPoolManager) must be recoverable
    by SandboxManager via the same per-pod Secret, not just the auth token."""
    from boxkite.tls import generate_pod_self_signed_cert

    manager = SandboxManager()
    pod_name = "sandbox-warm-tls-claim"
    secret_name = sidecar_auth_secret_name(pod_name)
    cert_pem, key_pem = generate_pod_self_signed_cert(pod_name)
    manager._k8s_core_api = _FakeCoreApi(
        secrets={
            secret_name: SimpleNamespace(
                data={
                    SIDECAR_AUTH_SECRET_KEY: base64.b64encode(b"warm-token").decode("ascii"),
                    SIDECAR_TLS_CERT_SECRET_KEY: base64.b64encode(cert_pem.encode()).decode("ascii"),
                    SIDECAR_TLS_KEY_SECRET_KEY: base64.b64encode(key_pem.encode()).decode("ascii"),
                }
            )
        },
    )

    recovered = await manager._ensure_pod_tls_cert_cached(pod_name)
    assert recovered == cert_pem.strip()
    assert manager._get_pod_tls_cert(pod_name) == cert_pem.strip()


@pytest.mark.asyncio
async def test_ensure_pod_secret_cached_returns_both_values_from_one_read():
    """The combined fetch (issue #178) must return both the auth token AND
    the TLS cert from a single Secret containing both keys, and cache both."""
    from boxkite.tls import generate_pod_self_signed_cert

    manager = SandboxManager()
    pod_name = "sandbox-combined-secret-fetch"
    secret_name = sidecar_auth_secret_name(pod_name)
    cert_pem, key_pem = generate_pod_self_signed_cert(pod_name)
    manager._k8s_core_api = _FakeCoreApi(
        secrets={
            secret_name: SimpleNamespace(
                data={
                    SIDECAR_AUTH_SECRET_KEY: base64.b64encode(b"combined-token").decode("ascii"),
                    SIDECAR_TLS_CERT_SECRET_KEY: base64.b64encode(cert_pem.encode()).decode("ascii"),
                    SIDECAR_TLS_KEY_SECRET_KEY: base64.b64encode(key_pem.encode()).decode("ascii"),
                }
            )
        },
    )

    token, recovered_cert = await manager._ensure_pod_secret_cached(pod_name)

    assert token == "combined-token"
    assert recovered_cert == cert_pem.strip()
    assert len(manager._k8s_core_api.secret_read_calls) == 1
    assert manager._get_pod_auth_token(pod_name) == "combined-token"
    assert manager._get_pod_tls_cert(pod_name) == cert_pem.strip()


@pytest.mark.asyncio
async def test_ensure_pod_secret_cached_returns_empty_strings_on_404():
    """A pod with no sidecar-auth Secret at all (never created, or already
    deleted) must return ("", "") rather than raising."""
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()

    token, cert = await manager._ensure_pod_secret_cached("sandbox-no-secret-at-all")

    assert token == ""
    assert cert == ""


@pytest.mark.asyncio
async def test_delete_pod_evicts_cached_tls_cert():
    manager = SandboxManager()
    manager._k8s_core_api = _FakeCoreApi()
    manager._cache_pod_tls_cert("sandbox-to-delete", "fake-cert-pem")

    await manager._delete_pod("sandbox-to-delete")

    assert manager._get_pod_tls_cert("sandbox-to-delete") == ""
