"""Tests for the opt-in fast warm-pod claim path (BOXKITE_FAST_CLAIM_ENABLED).

The fast path pops a claim-ready pod from WarmPoolManager's in-memory ready
index (populated off the existing background scan) and claims it via the same
compare-and-swap label patch the list-based path uses -- but WITHOUT the
per-request pod LIST or the per-pod Secret READ. The CAS stays the source of
truth: a stale/lost index entry fails the CAS and the caller falls back to the
unchanged list-based path.

Covers: index populate + pop, pop-when-empty, fast-claim success, fast-claim
CAS-409-retry, empty-index fallback to list, and flag-OFF byte-identical
behavior.
"""

from __future__ import annotations

import base64
from collections import deque
from types import SimpleNamespace

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

import boxkite.warm_pool as warm_pool_module
from boxkite.manager import SandboxManager
from boxkite.resource_config import BOXKITE_FAST_CLAIM_ENABLED_ENV
from boxkite.sidecar_auth import SIDECAR_AUTH_SECRET_KEY, sidecar_auth_secret_name
from boxkite.tls import SIDECAR_TLS_CERT_SECRET_KEY
from boxkite.warm_pool import ReadyPod, WarmPoolManager

from test_manager import _FakeCoreApi

pytestmark = pytest.mark.pr


def _make_warm_pod(
    *,
    pod_name: str,
    pod_ip: str = "10.8.0.90",
    resource_version: str = "1",
    size: str = "small",
):
    """A Running, all-containers-ready, young warm pod as the K8s API would
    return it -- the shape _scan_pool_state classifies as claim-ready."""
    return SimpleNamespace(
        metadata=SimpleNamespace(
            name=pod_name,
            resource_version=resource_version,
            creation_timestamp=None,
            annotations={},
            labels={
                "app": "sandbox",
                "pool": "warm",
                "sandbox.boxkite.dev/status": "warm",
                "sandbox.boxkite.dev/size": size,
            },
        ),
        status=SimpleNamespace(
            phase="Running",
            pod_ip=pod_ip,
            container_statuses=[SimpleNamespace(ready=True)],
        ),
    )


def _secret_for(pod_name: str, token: str, cert: str = "") -> tuple[str, SimpleNamespace]:
    data = {SIDECAR_AUTH_SECRET_KEY: base64.b64encode(token.encode()).decode("ascii")}
    if cert:
        data[SIDECAR_TLS_CERT_SECRET_KEY] = base64.b64encode(cert.encode()).decode("ascii")
    return sidecar_auth_secret_name(pod_name), SimpleNamespace(data=data)


# ---------------------------------------------------------------------------
# WarmPoolManager ready-index: populate + pop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_refresh_ready_index_populates_and_pop_returns_entry():
    """_refresh_ready_index builds per-size entries from scan candidates,
    reading each pod's token + cert once, and pop_ready_pod hands them back."""
    wp = WarmPoolManager()
    name, secret = _secret_for("sandbox-warm-a", "tok-a", cert="cert-a")
    wp._k8s_core_api = _FakeCoreApi(secrets={name: secret})

    await wp._refresh_ready_index([("sandbox-warm-a", "10.8.0.90", "7", "small")])

    popped = await wp.pop_ready_pod("small")
    assert popped == ReadyPod(
        pod_name="sandbox-warm-a",
        pod_ip="10.8.0.90",
        resource_version="7",
        size="small",
        auth_token="tok-a",
        tls_cert_pem="cert-a",
    )
    # Removed on pop: the same size is now empty.
    assert await wp.pop_ready_pod("small") is None


@pytest.mark.asyncio
async def test_pop_ready_pod_returns_none_when_empty():
    wp = WarmPoolManager()
    assert await wp.pop_ready_pod("small") is None
    assert await wp.pop_ready_pod("medium") is None


@pytest.mark.asyncio
async def test_refresh_ready_index_carries_token_forward_without_reread():
    """A pod already in the index keeps its precomputed token/cert on the
    next refresh -- its immutable Secret is not re-read every scan."""
    wp = WarmPoolManager()
    name, secret = _secret_for("sandbox-warm-b", "tok-b")
    fake = _FakeCoreApi(secrets={name: secret})
    wp._k8s_core_api = fake

    await wp._refresh_ready_index([("sandbox-warm-b", "10.8.0.91", "1", "small")])
    # resourceVersion advances but the pod persists -> no second secret read.
    await wp._refresh_ready_index([("sandbox-warm-b", "10.8.0.91", "2", "small")])

    assert len(fake.secret_read_calls) == 1
    popped = await wp.pop_ready_pod("small")
    assert popped.auth_token == "tok-b"
    assert popped.resource_version == "2"


@pytest.mark.asyncio
async def test_scan_pool_state_emits_ready_candidates():
    """The scan surfaces claim-ready warm pods as (name, ip, rv, size) so the
    index can be rebuilt off it with no extra pod LIST."""
    wp = WarmPoolManager()
    wp._k8s_core_api = _FakeCoreApi(
        list_responses=[SimpleNamespace(items=[_make_warm_pod(pod_name="sandbox-warm-c", resource_version="9")])]
    )

    _, _, _, _, ready_candidates = await wp._scan_pool_state()

    assert ready_candidates == [("sandbox-warm-c", "10.8.0.90", "9", "small")]


# ---------------------------------------------------------------------------
# Fake warm pool for the manager-side fast-claim tests
# ---------------------------------------------------------------------------


class _FakeWarmPool:
    def __init__(self, ready_by_size: dict[str, list[ReadyPod]] | None = None):
        self._ready = {size: deque(entries) for size, entries in (ready_by_size or {}).items()}
        self.pop_calls: list[str] = []

    async def pop_ready_pod(self, size: str):
        self.pop_calls.append(size)
        dq = self._ready.get(size)
        if not dq:
            return None
        return dq.popleft()


def _install_fake_warm_pool(monkeypatch, fake: _FakeWarmPool) -> None:
    async def _get():
        return fake

    monkeypatch.setattr(warm_pool_module, "get_warm_pool", _get)


class _CASFakeCoreApi(_FakeCoreApi):
    """_FakeCoreApi that can fail the compare-and-swap label patch (a list
    body) for specific pods, while letting the async non-evictable annotate
    (a dict body) succeed."""

    def __init__(self, *, fail_cas_for=None, **kwargs):
        super().__init__(**kwargs)
        self._fail_cas_for = set(fail_cas_for or [])
        self.cas_patch_names: list[str] = []

    async def patch_namespaced_pod(self, **kwargs):
        if isinstance(kwargs.get("body"), list):
            name = kwargs.get("name")
            self.cas_patch_names.append(name)
            if name in self._fail_cas_for:
                raise ApiException(status=409)
            return None
        return await super().patch_namespaced_pod(**kwargs)


# ---------------------------------------------------------------------------
# Fast-claim: success, CAS-retry, empty-index fallback
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fast_claim_success_returns_popped_pod_without_list_or_secret_read(monkeypatch):
    monkeypatch.setenv(BOXKITE_FAST_CLAIM_ENABLED_ENV, "true")
    manager = SandboxManager()
    manager._k8s_core_api = _CASFakeCoreApi()
    ready = ReadyPod(
        pod_name="sandbox-warm-fast",
        pod_ip="10.8.0.92",
        resource_version="3",
        size="small",
        auth_token="fast-tok",
        tls_cert_pem="",
    )
    fake_wp = _FakeWarmPool({"small": [ready]})
    _install_fake_warm_pool(monkeypatch, fake_wp)

    claimed = await manager._claim_warm_pod_via_k8s(size="small")

    assert claimed == ("sandbox-warm-fast", "10.8.0.92")
    # Hot path removed both round-trips: no pod LIST, no Secret READ.
    assert manager._k8s_core_api.list_calls == []
    assert manager._k8s_core_api.secret_read_calls == []
    # Connect info seeded from the index entry.
    assert manager._get_pod_auth_token("sandbox-warm-fast") == "fast-tok"
    # CAS actually ran (it stays the source of truth).
    assert manager._k8s_core_api.cas_patch_names == ["sandbox-warm-fast"]
    # Popped out of the index.
    assert await fake_wp.pop_ready_pod("small") is None


@pytest.mark.asyncio
async def test_fast_claim_retries_next_candidate_on_cas_conflict(monkeypatch):
    monkeypatch.setenv(BOXKITE_FAST_CLAIM_ENABLED_ENV, "true")
    manager = SandboxManager()
    # First candidate loses the CAS race (409); second wins.
    manager._k8s_core_api = _CASFakeCoreApi(fail_cas_for=["sandbox-warm-lost"])
    lost = ReadyPod("sandbox-warm-lost", "10.8.0.93", "1", "small", "tok-lost", "")
    won = ReadyPod("sandbox-warm-won", "10.8.0.94", "2", "small", "tok-won", "")
    fake_wp = _FakeWarmPool({"small": [lost, won]})
    _install_fake_warm_pool(monkeypatch, fake_wp)

    claimed = await manager._claim_warm_pod_via_k8s(size="small")

    assert claimed == ("sandbox-warm-won", "10.8.0.94")
    assert manager._k8s_core_api.cas_patch_names == ["sandbox-warm-lost", "sandbox-warm-won"]
    assert manager._get_pod_auth_token("sandbox-warm-won") == "tok-won"
    # Still no list-based fallback needed.
    assert manager._k8s_core_api.list_calls == []


@pytest.mark.asyncio
async def test_fast_claim_falls_back_to_list_when_index_empty(monkeypatch):
    monkeypatch.setenv(BOXKITE_FAST_CLAIM_ENABLED_ENV, "true")
    manager = SandboxManager()
    pod_name = "sandbox-warm-listed"
    secret_name, secret = _secret_for(pod_name, "list-tok")
    manager._k8s_core_api = _FakeCoreApi(
        list_responses=[SimpleNamespace(items=[_make_warm_pod(pod_name=pod_name, pod_ip="10.8.0.95")])],
        secrets={secret_name: secret},
    )
    fake_wp = _FakeWarmPool({"small": []})  # empty index
    _install_fake_warm_pool(monkeypatch, fake_wp)

    claimed = await manager._claim_warm_pod_via_k8s(size="small")

    # Empty index -> the unchanged list-based path claims the pod.
    assert claimed == (pod_name, "10.8.0.95")
    assert fake_wp.pop_calls == ["small"]  # fast path was attempted...
    assert len(manager._k8s_core_api.list_calls) == 1  # ...then fell back to LIST.
    assert manager._get_pod_auth_token(pod_name) == "list-tok"


# ---------------------------------------------------------------------------
# Flag OFF: behavior byte-identical to the list-based path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flag_off_uses_list_path_and_never_touches_index(monkeypatch):
    monkeypatch.delenv(BOXKITE_FAST_CLAIM_ENABLED_ENV, raising=False)
    manager = SandboxManager()
    pod_name = "sandbox-warm-default"
    secret_name, secret = _secret_for(pod_name, "default-tok")
    manager._k8s_core_api = _FakeCoreApi(
        list_responses=[SimpleNamespace(items=[_make_warm_pod(pod_name=pod_name, pod_ip="10.8.0.96")])],
        secrets={secret_name: secret},
    )
    # Even if a warm pool with ready entries exists, the flag-off path must
    # never consult it.
    fake_wp = _FakeWarmPool({"small": [ReadyPod(pod_name, "10.8.0.96", "1", "small", "x", "")]})
    _install_fake_warm_pool(monkeypatch, fake_wp)

    claimed = await manager._claim_warm_pod_via_k8s(size="small")

    assert claimed == (pod_name, "10.8.0.96")
    assert len(manager._k8s_core_api.list_calls) == 1
    assert fake_wp.pop_calls == []  # index untouched
