"""Fault-injection coverage for GitHub issue #151, scenario 1: warm-pool
exhaustion combined with a failing (or slow) cold-create fallback.

`create_session` (`_manager_create.py`) tries a warm-pod claim first
(`_claim_warm_pod_via_k8s`, `_manager_warm_pool.py`) and falls back to a cold
`_create_pod` (`manager.py`) only when the pool has nothing eligible. Nothing
in this repo's existing test suite exercises what happens when *both* of
those fail in the same call -- `test_manager.py`'s warm-pool tests each cover
one side (an empty/exhausted pool, or a cold-create failure) in isolation.
This file's job is the combination: does the caller get a clear, typed
exception, or does something hang / leave a half-created session behind.

Uses the same `_FakeCoreApi` fake-K8s-client pattern `test_manager.py` and
`test_manager_secrets_network_policy.py` already use -- no real cluster.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from boxkite.manager import SandboxManager
from test_manager import _FakeCoreApi

pytestmark = pytest.mark.pr


async def test_create_session_raises_clear_error_when_warm_pool_empty_and_cold_create_fails(
    monkeypatch,
):
    """Empty warm pool (a real `_FakeCoreApi` scan finding zero eligible
    pods, exercising `_claim_warm_pod_via_k8s`'s real code) plus a cold
    `_create_pod` that raises -- the exact combination no existing test
    covers. The caller must see the underlying, typed exception (not a
    generic swallow-and-hang), and no session bookkeeping should be left
    half-populated for a session_id whose pod was never actually created.
    """
    manager = SandboxManager()
    manager._k8s_initialized = True
    # An empty `pool=warm` label scan -- _claim_warm_pod_via_k8s's own
    # for-loop over `pods.items` simply never executes, so it returns None
    # exactly like a real exhausted pool. _resolve_session's own "does this
    # session already have a running pod" pre-check is bypassed via a direct
    # monkeypatch below (same pattern test_manager.py's own create_session
    # tests use) so this one scripted response is consumed exactly once, by
    # the warm-pool claim itself -- the thing this test is actually about.
    manager._k8s_core_api = _FakeCoreApi(list_responses=[SimpleNamespace(items=[])])

    async def fake_resolve_session(_target_session_id):
        raise ValueError("No running pod found for session")

    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)

    async def fake_create_pod(*_args, **_kwargs):
        raise ApiException(status=500, reason="Internal error creating pod")

    monkeypatch.setattr(manager, "_create_pod", fake_create_pod)

    org_id = uuid4()
    session_id = "session-warm-pool-exhausted"

    with pytest.raises(ApiException):
        await manager.create_session(organization_id=org_id, session_id=session_id)

    # No partial/corrupted session record: _record_session_metadata and
    # _cache_session_endpoint only ever run after a pod exists, so a session
    # that never got one must not resolve as if it were live.
    assert session_id not in manager._session_endpoints


async def test_create_session_does_not_hang_and_lock_is_reusable_after_a_failed_attempt(
    monkeypatch,
):
    """Regression guard for the other half of "clear error, not a hang":
    the per-session create lock (`_get_session_create_lock`) must be
    released even when the body raises, so a second attempt for the exact
    same session_id -- e.g. the caller's own retry after the first 500 --
    can still proceed and succeed once the underlying capacity issue clears,
    rather than deadlocking behind a lock the failed attempt never freed.
    """
    manager = SandboxManager()
    manager._k8s_initialized = True
    manager._k8s_core_api = SimpleNamespace(patch_namespaced_pod=AsyncMock())

    async def fake_resolve_session(_target_session_id):
        raise ValueError("No running pod found for session")

    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)

    async def fake_claim_returns_nothing(size="small"):
        return None

    monkeypatch.setattr(manager, "_claim_warm_pod_via_k8s", fake_claim_returns_nothing)

    async def fake_create_pod_fails(*_args, **_kwargs):
        raise TimeoutError("Pod sandbox-x not ready after 90s")

    monkeypatch.setattr(manager, "_create_pod", fake_create_pod_fails)

    org_id = uuid4()
    session_id = "session-retry-after-failure"

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            manager.create_session(organization_id=org_id, session_id=session_id),
            timeout=5,
        )

    # Capacity issue resolves; retry the identical session_id.
    fake_configure_response = SimpleNamespace(
        raise_for_status=lambda: None, json=lambda: {"prefetched_files": []}
    )
    fake_http_client = SimpleNamespace(post=AsyncMock(return_value=fake_configure_response))

    async def fake_create_pod_succeeds(*_args, **_kwargs):
        return "10.8.0.42"

    monkeypatch.setattr(manager, "_create_pod", fake_create_pod_succeeds)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_http_client)

    result = await asyncio.wait_for(
        manager.create_session(organization_id=org_id, session_id=session_id),
        timeout=5,
    )
    assert result["pod_name"]
