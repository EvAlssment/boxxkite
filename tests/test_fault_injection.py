"""Chaos / fault-injection tests for SandboxManager and WarmPoolManager --
GitHub issue #151.

Companion to tests/test_sidecar_process_restart_survival.py, which covers
sidecar-process-level crash recovery via fakes/mocks rather than a real
cluster. This module covers the two SandboxManager/WarmPoolManager-level
failure modes issue #151 calls out (the third, control-plane restart with
live sessions, is covered separately in
control-plane/tests/test_fault_injection.py since it exercises the FastAPI
app/DB, not this package):

1. Warm-pool exhaustion: `create_session` when the size's warm sub-pool is
   empty. Uses the exact `_FakeCoreApi` fake K8s API test_manager.py already
   uses for pod-lifecycle tests (not a real cluster).
2. Pod eviction mid-session: an in-flight `execute()` call when the pod's
   sidecar has become unreachable (simulated as a connection-level httpx
   error, the same signal a real node-pressure eviction or drain produces).

Every test here bounds its await with asyncio.wait_for so a real hang (the
bug class this issue is specifically about) fails the test instead of
wedging the suite.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from kubernetes_asyncio.client.exceptions import ApiException

from boxkite.manager import SandboxManager
from boxkite.sidecar_auth import sidecar_auth_secret_name
from test_manager import _FakeCoreApi

pytestmark = pytest.mark.pr

# Generous bound for fault-injection assertions: real work here is all
# fake/in-memory, so anything taking anywhere near this long is a hang, not
# slowness.
FAULT_TIMEOUT = 5


def _empty_warm_pool_api() -> _FakeCoreApi:
    """A _FakeCoreApi whose warm-pod listing always comes back empty --
    simulates the size's warm sub-pool being fully exhausted (all pods
    already claimed, or WARM_POOL_SIZE_* configured to 0 for that size)."""
    return _FakeCoreApi(list_responses=[SimpleNamespace(items=[])])


def _wire_fake_core_api(manager: SandboxManager, fake_core_api: _FakeCoreApi) -> None:
    """Install `fake_core_api` and mark K8s as already initialized so
    methods that lazily call `_init_k8s()` (e.g. `_create_k8s_session`,
    `_get_session_metadata`) don't clobber it by connecting to whatever
    real cluster this machine's kubeconfig happens to point at."""
    manager._k8s_core_api = fake_core_api
    manager._k8s_initialized = True


# ---------------------------------------------------------------------------
# 1(a). Warm-pool exhaustion -> cold-create succeeds
# ---------------------------------------------------------------------------


async def test_claim_warm_pod_returns_none_when_pool_is_empty():
    """The exhaustion signal itself: an empty warm pool must return None
    quickly, not raise, and not hang -- callers (_create_k8s_session) depend
    on None meaning "fall back to cold create", not on an exception."""
    manager = SandboxManager()
    _wire_fake_core_api(manager, _empty_warm_pool_api())

    claimed = await asyncio.wait_for(manager._claim_warm_pod_via_k8s(), timeout=FAULT_TIMEOUT)

    assert claimed is None


async def test_warm_pool_exhausted_falls_back_to_cold_create_cleanly(monkeypatch):
    """End-to-end: create_session() against an empty warm pool must fall
    back to a cold pod create and succeed cleanly -- one warm-pod list
    attempt (that finds nothing), one cold pod create, no hang, no
    unhandled exception."""
    manager = SandboxManager()
    fake_core_api = _empty_warm_pool_api()
    _wire_fake_core_api(manager, fake_core_api)

    async def fake_wait_for_pod_ready(_pod_name):
        return "10.9.0.5"

    class _FakeConfigureResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"prefetched_files": []}

    class _FakeHttpClient:
        async def post(self, path, json=None, **_kwargs):
            assert path == "/configure"
            return _FakeConfigureResponse()

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: _FakeHttpClient())

    result = await asyncio.wait_for(
        manager._create_k8s_session(
            organization_id=uuid4(),
            session_id="session-warm-exhausted",
            work_item_id=uuid4(),
        ),
        timeout=FAULT_TIMEOUT,
    )

    assert result["pod_name"]
    # Exactly one warm-pod list attempt (found nothing) then one cold create --
    # no retry storm, no duplicate pod creation.
    assert len(fake_core_api.list_calls) == 1
    assert len(fake_core_api.create_calls) == 1


# ---------------------------------------------------------------------------
# 1(b). Warm-pool exhaustion -> cold-create ALSO fails
# ---------------------------------------------------------------------------


async def test_cold_create_api_error_propagates_cleanly_without_hanging(monkeypatch):
    """create_namespaced_pod itself failing (e.g. quota exceeded, apiserver
    503) with no pod ever created must propagate a clear exception quickly --
    not hang, not swallow the error and report fake success."""
    manager = SandboxManager()
    _wire_fake_core_api(manager, _FakeCoreApi())

    async def failing_create_namespaced_pod(**_kwargs):
        raise ApiException(status=500, reason="internal error")

    monkeypatch.setattr(manager._k8s_core_api, "create_namespaced_pod", failing_create_namespaced_pod)

    with pytest.raises(ApiException):
        await asyncio.wait_for(
            manager._create_pod(
                pod_name="sandbox-cold-fails",
                session_id="session-cold-fails",
                organization_id=uuid4(),
                work_item_id=uuid4(),
            ),
            timeout=FAULT_TIMEOUT,
        )


async def test_cold_create_cleans_up_pod_when_it_never_becomes_ready(monkeypatch):
    """Regression test for a real gap found while writing this fault-
    injection suite: when the pod is created but never becomes Ready (the
    other half of "cold-create also fails" -- e.g. no schedulable node, an
    image pull failure, or the pod being evicted before it ever goes
    Running), _wait_for_pod_ready raises -- but nothing deleted the pod
    that WAS already created, leaking it (and its sidecar-auth Secret)
    forever. WarmPoolManager._create_warm_pod already gets this right (see
    warm_pool.py); this exercises the equivalent SandboxManager cold-create
    path and pins the fix: the failed pod (and its Secret) must be cleaned
    up before the exception propagates, so a warm-pool-exhaustion cold
    create that then also fails doesn't quietly accumulate stuck pods."""
    manager = SandboxManager()
    fake_core_api = _FakeCoreApi()
    _wire_fake_core_api(manager, fake_core_api)

    async def fake_wait_for_pod_ready(_pod_name):
        raise TimeoutError("Pod sandbox-cold-stuck not ready after 60s")

    monkeypatch.setattr(manager, "_wait_for_pod_ready", fake_wait_for_pod_ready)

    with pytest.raises(TimeoutError):
        await asyncio.wait_for(
            manager._create_pod(
                pod_name="sandbox-cold-stuck",
                session_id="session-cold-stuck",
                organization_id=uuid4(),
                work_item_id=uuid4(),
            ),
            timeout=FAULT_TIMEOUT,
        )

    assert fake_core_api.create_calls, "sanity: the pod create call must have actually happened"
    assert [call["name"] for call in fake_core_api.delete_calls] == ["sandbox-cold-stuck"]
    assert [call["name"] for call in fake_core_api.secret_delete_calls] == [
        sidecar_auth_secret_name("sandbox-cold-stuck")
    ]


# ---------------------------------------------------------------------------
# 2. Pod eviction mid-session (in-flight exec)
# ---------------------------------------------------------------------------


async def test_exec_recovers_after_pod_eviction_by_recreating_the_session(monkeypatch):
    """Simulates the exact scenario in the issue: an in-flight exec() call
    whose pod gets evicted mid-request (surfaced here as a connection-level
    httpx error, the same signal a real node-pressure eviction produces).
    Expected, already-implemented behavior: the manager detects the
    transport failure, recreates the session on a fresh pod using cached
    metadata, and transparently retries -- the caller gets a correct result,
    not a hang and not a silently stale/invalid session."""
    manager = SandboxManager()
    session_id = "session-evicted"
    attempts = {"count": 0}

    async def fake_resolve_session(_session_id):
        return ("evicted-pod", "10.9.0.9")

    class _FakeHttpClient:
        async def post(self, path, json=None, **_kwargs):
            assert path == "/exec"
            attempts["count"] += 1
            if attempts["count"] == 1:
                raise httpx.ConnectError("connection refused: pod evicted")
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"exit_code": 0, "stdout": "ok", "stderr": ""},
            )

    recovered_sessions = []

    async def fake_session_sidecar_available(_session_id):
        return False

    async def fake_recover_session(target_session_id, _error):
        recovered_sessions.append(target_session_id)

    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: _FakeHttpClient())
    monkeypatch.setattr(manager, "_session_sidecar_available", fake_session_sidecar_available)
    monkeypatch.setattr(manager, "_recover_session_after_sidecar_error", fake_recover_session)

    result = await asyncio.wait_for(
        manager.execute(session_id, "echo hi"),
        timeout=FAULT_TIMEOUT,
    )

    assert result == {"exit_code": 0, "stdout": "ok", "stderr": ""}
    assert recovered_sessions == [session_id]
    assert attempts["count"] == 2


async def test_exec_during_eviction_raises_clear_error_when_session_is_unrecoverable(monkeypatch):
    """The other half of "surfaces a clear error rather than hanging or
    silently succeeding with an invalid session": if the pod is evicted AND
    there is no metadata left to recover from (pod gone from K8s, no
    SessionMetadataStore configured -- the default), recovery must fail
    loudly and quickly with a clear error, never hang and never let the
    caller believe the retried call ran against a real session."""
    manager = SandboxManager()
    _wire_fake_core_api(manager, _FakeCoreApi(list_responses=[SimpleNamespace(items=[])]))
    session_id = "session-unrecoverable"

    async def fake_resolve_session(_session_id):
        return ("evicted-pod", "10.9.0.9")

    class _FakeHttpClient:
        async def post(self, path, json=None, **_kwargs):
            raise httpx.ConnectError("connection refused: pod evicted")

    async def fake_session_sidecar_available(_session_id):
        return False

    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: _FakeHttpClient())
    monkeypatch.setattr(manager, "_session_sidecar_available", fake_session_sidecar_available)
    # NoOpSessionMetadataStore is the default -- reconstruct() always
    # returns None -- and _get_session_metadata's real K8s lookup above
    # returns no pods either, so recovery has nothing to work with.

    with pytest.raises(ValueError, match="not found"):
        await asyncio.wait_for(
            manager.execute(session_id, "echo hi"),
            timeout=FAULT_TIMEOUT,
        )


async def test_concurrent_execs_during_eviction_do_not_deadlock(monkeypatch):
    """Two concurrent exec() calls hitting the same evicted pod at once
    must not deadlock each other via the per-session recovery lock -- one
    performs the recovery, the other waits for it and reuses the result,
    and both complete within the bound.

    Interleaving is forced deterministically with an asyncio.Event (the
    same technique test_manager.py's
    test_call_sidecar_with_recovery_serializes_concurrent_skill_replays
    uses) rather than relying on asyncio.gather's incidental scheduling --
    neither fake call below awaits anything real, so without an explicit
    suspension point the first task would simply run to completion before
    the second ever started, which would not actually exercise the lock's
    concurrent-waiter path at all.
    """
    manager = SandboxManager()
    session_id = "session-concurrent-eviction"
    recover_calls = []
    second_caller_attempted = asyncio.Event()

    async def fake_resolve_session(_session_id):
        return ("evicted-pod", "10.9.0.9")

    exec_attempts = {"count": 0}

    class _FakeHttpClient:
        async def post(self, path, json=None, **_kwargs):
            exec_attempts["count"] += 1
            attempt = exec_attempts["count"]
            if attempt == 2:
                second_caller_attempted.set()
            if attempt <= 2:
                raise httpx.ConnectError("connection refused: pod evicted")
            return SimpleNamespace(
                raise_for_status=lambda: None,
                json=lambda: {"exit_code": 0, "stdout": "ok", "stderr": ""},
            )

    availability_checks = {"count": 0}

    async def fake_session_sidecar_available(_session_id):
        availability_checks["count"] += 1
        if availability_checks["count"] == 1:
            # The first caller to reach the recovery lock deliberately
            # waits here for the second caller's own initial failed
            # attempt, forcing genuine concurrency through the lock
            # instead of one caller finishing before the other starts.
            await asyncio.wait_for(second_caller_attempted.wait(), timeout=FAULT_TIMEOUT)
            return False
        # The second caller reaches this only after the first has released
        # the recovery lock (i.e. after recovery already happened).
        return True

    async def fake_recover_session(target_session_id, _error):
        recover_calls.append(target_session_id)

    monkeypatch.setattr(manager, "_resolve_session", fake_resolve_session)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: _FakeHttpClient())
    monkeypatch.setattr(manager, "_session_sidecar_available", fake_session_sidecar_available)
    monkeypatch.setattr(manager, "_recover_session_after_sidecar_error", fake_recover_session)

    results = await asyncio.wait_for(
        asyncio.gather(
            manager.execute(session_id, "echo one"),
            manager.execute(session_id, "echo two"),
        ),
        timeout=FAULT_TIMEOUT,
    )

    assert all(r == {"exit_code": 0, "stdout": "ok", "stderr": ""} for r in results)
    # Recovery ran exactly once even though both callers raced into it.
    assert recover_calls == [session_id]
