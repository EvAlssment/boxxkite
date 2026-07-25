"""Fault-injection coverage for GitHub issue #151, scenario 2: a pod that
becomes unreachable (IP no longer routable, or the pod itself gone) in the
middle of an in-flight sidecar call -- the shape of a node-pressure eviction,
not a graceful `destroy_session`.

`_call_sidecar_with_recovery` (`_manager_recovery.py`) is the single retry/
recovery path every `SidecarProxyMixin` method (`execute`, `file_create`,
etc., in `_manager_proxy.py`) routes through: a retryable transport error
(`httpx.ConnectError` and friends, per `_is_retryable_sidecar_error`)
triggers one best-effort session recreation, then one retry. The existing
tests for this helper (`test_manager.py`'s
`test_call_sidecar_with_recovery_replays_cached_skills_*`) all mock recovery
itself and only check the skills-replay bookkeeping around it. None of them
answer this repo's actual open question: what does a caller of `execute()`
observe when the pod is genuinely gone and recovery ALSO cannot get a
replacement running -- a clean, typed exception, or a hang / an endpoint
cache stuck pointing at a dead pod forever?

No real Kubernetes cluster or pod is used here -- a fake HTTP client
raising `httpx.ConnectError` on every call stands in for "this pod's IP is
no longer routable", exactly like `_is_retryable_sidecar_error` classifies
a real node-eviction symptom.
"""

from uuid import uuid4

import httpx
import pytest

from boxkite.manager import SandboxManager

pytestmark = pytest.mark.pr


class _UnreachablePodHttpClient:
    """Every call fails the way an actual unreachable pod IP does: the TCP
    connect itself never completes. Stands in for both the sidecar
    operation call and `_session_sidecar_available`'s own `/health` probe,
    since a truly evicted pod fails both identically.
    """

    async def get(self, _path, timeout=None):
        raise httpx.ConnectError("Connection refused")

    async def post(self, _path, json=None):
        raise httpx.ConnectError("Connection refused")


async def test_execute_raises_clear_error_when_pod_evicted_and_replacement_also_fails(
    monkeypatch,
):
    """The realistic node-pressure-eviction shape: the original pod's IP is
    unreachable AND the replacement pod recovery tries to spin up also fails
    to schedule (the same underlying node pressure, or another cold-create
    failure per test_chaos_warm_pool_exhaustion.py). The caller must see a
    single, clear exception -- not a hang, and not the original
    ConnectError swallowed into something misleading.
    """
    manager = SandboxManager()
    session_id = "session-evicted-no-replacement"
    org_id = uuid4()
    manager._cache_session_endpoint(session_id, "evicted-pod", "10.8.0.50")

    async def fake_resolve_session(target_session_id):
        assert target_session_id == session_id
        # The cached endpoint still points at the pod that no longer
        # exists -- exactly the "session marked active with a pod that's
        # gone" risk this scenario is checking for.
        return ("evicted-pod", "10.8.0.50")

    async def fake_get_session_metadata(target_session_id):
        assert target_session_id == session_id
        return {"organization_id": org_id, "work_item_id": None, "upload_file_ids": []}

    async def fake_create_session(**_kwargs):
        # Recovery's own attempt to stand up a replacement pod fails too --
        # e.g. the same node pressure that evicted the original pod is also
        # blocking a fresh schedule.
        raise RuntimeError("no schedulable node for replacement pod")

    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: _UnreachablePodHttpClient())
    monkeypatch.setattr(manager, "_get_session_metadata", fake_get_session_metadata)
    monkeypatch.setattr(manager, "create_session", fake_create_session)

    with pytest.raises(RuntimeError, match="no schedulable node"):
        await manager.execute(session_id, "echo hi")

    # Bookkeeping must not be left inconsistent: _recover_session_after_
    # sidecar_error invalidates the stale endpoint cache BEFORE attempting
    # the replacement pod, specifically so a session known to be broken is
    # never left "active" pointing at a pod that no longer exists, even
    # though standing up the replacement itself failed.
    assert manager._get_cached_session_endpoint(session_id) is None

    # The per-session recovery lock must not be left held/stuck -- a later
    # call for the same session_id must be able to acquire it rather than
    # hang forever behind a lock this failed recovery never released.
    assert session_id not in manager._recovery_locks


async def test_execute_raises_clear_error_when_pod_evicted_with_no_recovery_metadata(
    monkeypatch,
):
    """Harsher variant: the pod is gone AND this manager process has no
    metadata to reconstruct it from at all (no SessionMetadataStore
    configured, which is this repo's default -- see
    `_reconstruct_session_metadata_from_db`'s own docstring). This is the
    real behavior of a fresh `SandboxManager()` with no K8s cluster reachable
    in this test environment, exercised with no mocking of the metadata
    lookup itself.

    Documents a real, disclosed gap found while writing this test: because
    `_recover_session_after_sidecar_error` only calls
    `_invalidate_session_endpoint` AFTER it confirms metadata exists, a
    session with NO recoverable metadata at all keeps its stale cache entry
    around after this failure (it only gets cleared lazily, the next time
    something calls the real, K8s-backed `_resolve_session` and finds the
    cached pod truly gone). Not fixed here per this task's explicit
    instruction not to fabricate coverage or silently patch unrelated
    behavior discovered along the way -- flagged for a follow-up issue.

    `_init_k8s` is stubbed to a no-op (leaving `_k8s_core_api` at its
    default `None`) so `_get_session_metadata` deterministically returns
    None -- letting the real kube-config discovery run here would make this
    test's timing and outcome depend on whatever kubeconfig happens to be
    on the machine running it (a real cluster reachable in CI vs. a stray
    local kubeconfig on a dev laptop), not on the fault this test exists to
    exercise.
    """
    manager = SandboxManager()
    session_id = "session-evicted-no-metadata"
    manager._cache_session_endpoint(session_id, "evicted-pod", "10.8.0.51")

    async def fake_resolve_session(target_session_id):
        assert target_session_id == session_id
        return ("evicted-pod", "10.8.0.51")

    async def fake_init_k8s():
        return None

    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)
    monkeypatch.setattr(manager, "_init_k8s", fake_init_k8s)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: _UnreachablePodHttpClient())

    with pytest.raises(ValueError, match="not found for recovery"):
        await manager.execute(session_id, "echo hi")

    # Known, disclosed gap (see docstring above): the stale cache entry is
    # NOT proactively cleared on this particular failure path.
    assert manager._get_cached_session_endpoint(session_id) == ("evicted-pod", "10.8.0.51")

    assert session_id not in manager._recovery_locks
