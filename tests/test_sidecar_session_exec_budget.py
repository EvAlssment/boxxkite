"""Tests for the per-session cumulative exec budget (GitHub issue #122).

Unlike ExecRequest.timeout (bounds one /exec call) or max_runtime_seconds
(bounds one background process), nothing previously bounded the cumulative
exec count or wall-clock exec time across a whole session -- an agent stuck
retrying the same failing command forever would run forever, each
individual call finishing well inside its own timeout.

A follow-up security review of the first implementation (which wired the
budget into /exec only) found two CRITICAL gaps and one MEDIUM race, all
fixed here:

  - /interpreter/exec (sidecar_interpreter.py) and /process/start
    (sidecar_processes.py) are both default-enabled, same as bash_tool, but
    never touched the budget at all -- a session looping via either one
    spent zero budget and was never throttled. They now share the exact
    same counters/sticky flag as /exec (see
    `_reserve_session_exec_slot_or_raise`/
    `_record_session_exec_duration_or_raise` in main.py).
  - A session that had already tripped the sticky `_session_budget_exceeded`
    flag via /exec could previously start a brand-new interpreter call or
    background process completely unobstructed, since neither route read
    that flag. Both routes now check it first, before doing any work.
  - The exec-count precheck and its increment used to be separated by an
    unguarded `await` (the command's own execution), so N concurrent calls
    near the ceiling could all pass the precheck before any of them
    recorded its usage, overshooting the ceiling. The precheck and the
    increment are now one lock-guarded atomic step.

These tests exercise the real /exec, /interpreter/exec, and /process/start
routes via TestClient (same pattern as test_sidecar_exec_secret_env.py,
test_sidecar_interpreter.py, and test_sidecar_process_sessions.py --
exec_in_sandbox/the interpreter spawn/the process spawn are faked or
redirected to a local shell so the tests are fast and don't require
nsenter/docker), plus the budget helper functions directly.
"""

from __future__ import annotations

import asyncio

import sys

import main as sidecar_main
import sidecar_lsp
from fastapi import HTTPException
from fastapi.testclient import TestClient
from test_sidecar_lsp import _write_fake_driver

AUTH_TOKEN = "the-real-secret"


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: AUTH_TOKEN}


def _reset_budget_state():
    sidecar_main._session_exec_count = 0
    sidecar_main._session_exec_seconds = 0.0
    sidecar_main._session_budget_exceeded = None
    sidecar_main._session_budget_lock = None
    # Also reset the other exec-like routes' own live state, since these
    # tests now exercise /interpreter/exec, /process/start, and /lsp/*
    # too.
    sidecar_main._process_registry.clear()
    sidecar_main._exposed_ports.clear()
    sidecar_main._process_registry_lock = None
    sidecar_main._interpreter_handle = None
    sidecar_main._interpreter_lock = None
    sidecar_main._lsp_registry.clear()
    sidecar_main._lsp_registry_lock = None


def setup_function(_):
    _reset_budget_state()


def teardown_function(_):
    _reset_budget_state()


def _fake_exec_in_sandbox_factory(exit_code=0, stdout="ok", stderr=""):
    async def _fake_exec_in_sandbox(command, timeout, extra_env=None):
        return (exit_code, stdout, stderr)

    return _fake_exec_in_sandbox


def _bypass_nsenter_for_interpreter(monkeypatch):
    """Route the persistent Python interpreter's spawn command straight to
    a local shell -- same technique tests/test_sidecar_interpreter.py uses.
    """
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 1)
    monkeypatch.setattr(
        sidecar_main, "build_k8s_exec_command", lambda pid, command: ["sh", "-c", command]
    )


async def _fake_spawn_background_process(command: str, *, expose_network: bool = False):
    """Bypass nsenter/docker-exec entirely -- same technique
    tests/test_sidecar_process_sessions.py uses."""
    return await asyncio.create_subprocess_shell(
        command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


async def _noop_periodic_sync_loop():
    return


def _disable_periodic_sync(monkeypatch):
    """Neutralize the background periodic-sync task main.py's startup_event
    creates on every ASGI lifespan start (i.e. every `with TestClient(...)`).

    Unrelated to the exec budget itself, but a real, pre-existing fragility
    in this test environment that using `with TestClient()` here (needed to
    keep the persistent interpreter/background process alive across
    multiple calls within one test, same as test_sidecar_interpreter.py and
    test_sidecar_process_sessions.py already do) can trigger: if ANY prior
    `with TestClient()` anywhere in the same pytest process ever failed to
    cleanly gather that task on its own shutdown (e.g. a task reference
    left over from a previous, already-closed event loop), shutdown_event
    raises before it reaches the line that resets `_periodic_sync_task`
    back to None -- so `main._periodic_sync_task` stays permanently
    "stuck" pointing at a dead task, `startup_event`'s
    `if _periodic_sync_task is None or _periodic_sync_task.done()` guard
    never re-creates it (a task bound to a now-closed loop never actually
    reaches `.done()`), and every SUBSEQUENT `with TestClient()` in the
    whole process inherits and re-fails on the same stuck reference,
    regardless of what it's actually testing. Forcibly clearing the
    reference here, in addition to no-op'ing the loop body, means this
    test gets a guaranteed-fresh (and guaranteed-instantly-done) task
    regardless of what any earlier, unrelated test left behind.
    """
    monkeypatch.setattr(sidecar_main, "_periodic_sync_loop", _noop_periodic_sync_loop)
    sidecar_main._periodic_sync_task = None


def test_exec_within_budget_returns_normal_response(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 5)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 3600.0)
    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox_factory())
    client = _client()

    response = client.post(
        "/exec", json={"command": "echo hi", "timeout": 5}, headers=_headers()
    )

    assert response.status_code == 200
    body = response.json()
    assert body["exit_code"] == 0
    assert body["stdout"] == "ok"
    assert sidecar_main._session_exec_count == 1
    assert sidecar_main._session_budget_exceeded is None


def test_exec_count_breach_rejects_the_call_that_would_exceed_it(monkeypatch):
    """With a ceiling of 2, exactly 2 execs succeed; the 3rd is refused
    before it ever runs (exec count is known upfront, unlike duration)."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 2)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 0.0)

    ran_commands = []

    async def _fake_exec_in_sandbox(command, timeout, extra_env=None):
        ran_commands.append(command)
        return (0, "ok", "")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox)
    client = _client()

    first = client.post("/exec", json={"command": "echo 1", "timeout": 5}, headers=_headers())
    second = client.post("/exec", json={"command": "echo 2", "timeout": 5}, headers=_headers())
    third = client.post("/exec", json={"command": "echo 3", "timeout": 5}, headers=_headers())

    assert first.status_code == 200
    assert second.status_code == 200
    assert third.status_code == 403
    assert ran_commands == ["echo 1", "echo 2"]  # the 3rd command never ran

    body = third.json()["detail"]
    assert body["error_type"] == "session_budget_exceeded"
    assert body["reason"] == "exec_count"
    assert body["limit"] == 2
    assert body["used"] == 2


def test_exec_count_breach_tears_down_live_session_resources(monkeypatch):
    """On breach, background processes and interpreters must be killed --
    the same cleanup /configure performs before wiping state for the next
    tenant, run here because the session can no longer be trusted."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 1)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 0.0)
    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox_factory())

    teardown_calls = []

    async def _fake_kill_all_processes():
        teardown_calls.append("kill_all_processes")
        return 0

    async def _fake_reset_interpreter():
        teardown_calls.append("reset_interpreter")

    async def _fake_reset_node_interpreter():
        teardown_calls.append("reset_node_interpreter")

    monkeypatch.setattr(sidecar_main, "_kill_all_processes", _fake_kill_all_processes)
    monkeypatch.setattr(sidecar_main, "_reset_interpreter", _fake_reset_interpreter)
    monkeypatch.setattr(sidecar_main, "_reset_node_interpreter", _fake_reset_node_interpreter)
    client = _client()

    first = client.post("/exec", json={"command": "echo 1", "timeout": 5}, headers=_headers())
    assert first.status_code == 200
    assert teardown_calls == []  # no breach yet -- budget of 1 not exceeded by the 1st call

    second = client.post("/exec", json={"command": "echo 2", "timeout": 5}, headers=_headers())
    assert second.status_code == 403
    assert teardown_calls == ["kill_all_processes", "reset_interpreter", "reset_node_interpreter"]


def test_exec_seconds_breach_is_reported_on_the_call_that_crosses_it(monkeypatch):
    """The cumulative-seconds ceiling can only be known AFTER a call runs
    (duration isn't known upfront) -- the call whose own duration crosses
    the ceiling gets the structured breach response instead of its normal
    ExecResponse. Uses a real (short) sleep in the faked exec so the
    route's own `time.monotonic()` measurement is genuine, rather than
    monkeypatching the global `time` module (which would also perturb
    TestClient's own internals)."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 0)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 0.05)

    async def _slow_fake_exec_in_sandbox(command, timeout, extra_env=None):
        await asyncio.sleep(0.15)
        return (0, "ok", "")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _slow_fake_exec_in_sandbox)
    client = _client()

    response = client.post("/exec", json={"command": "echo hi", "timeout": 5}, headers=_headers())

    assert response.status_code == 403
    body = response.json()["detail"]
    assert body["error_type"] == "session_budget_exceeded"
    assert body["reason"] == "exec_seconds"
    assert body["limit"] == 0.05
    assert body["used"] > 0.05


def test_budget_exceeded_is_sticky_and_rejects_without_running(monkeypatch):
    """Once tripped, every subsequent /exec is rejected the same way without
    re-running the teardown or the command -- until the next /configure."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    sidecar_main._session_budget_exceeded = {
        "reason": "exec_count",
        "limit": 5,
        "used": 5,
    }

    ran_commands = []

    async def _fake_exec_in_sandbox(command, timeout, extra_env=None):
        ran_commands.append(command)
        return (0, "ok", "")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox)
    client = _client()

    response = client.post("/exec", json={"command": "echo hi", "timeout": 5}, headers=_headers())

    assert response.status_code == 403
    assert ran_commands == []
    body = response.json()["detail"]
    assert body["error_type"] == "session_budget_exceeded"
    assert body["reason"] == "exec_count"


def test_zero_disables_both_checks(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 0)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 0.0)
    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox_factory())
    client = _client()

    for _ in range(10):
        response = client.post(
            "/exec", json={"command": "echo hi", "timeout": 5}, headers=_headers()
        )
        assert response.status_code == 200

    assert sidecar_main._session_exec_count == 10
    assert sidecar_main._session_budget_exceeded is None


def test_configure_resets_session_exec_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(sidecar_main, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(sidecar_main, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(sidecar_main, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(sidecar_main, "TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(sidecar_main, "prefetch_files", lambda *a, **k: _immediate([]))
    monkeypatch.setattr(sidecar_main.os, "chown", lambda *a, **k: None)

    sidecar_main._session_exec_count = 7
    sidecar_main._session_exec_seconds = 123.4
    sidecar_main._session_budget_exceeded = {"reason": "exec_count", "limit": 5, "used": 7}

    client = _client()
    response = client.post(
        "/configure",
        json={"session_id": "new-session", "organization_id": None, "work_item_id": None},
        headers=_headers(),
    )

    assert response.status_code == 200
    assert sidecar_main._session_exec_count == 0
    assert sidecar_main._session_exec_seconds == 0.0
    assert sidecar_main._session_budget_exceeded is None


async def _immediate(result):
    return result


def test_session_exec_count_breach_helper_returns_none_under_ceiling():
    _reset_budget_state()
    assert sidecar_main._session_exec_count_breach() is None


def test_session_exec_count_breach_helper_detects_ceiling(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 3)
    sidecar_main._session_exec_count = 3

    breach = sidecar_main._session_exec_count_breach()

    assert breach == {"reason": "exec_count", "limit": 3, "used": 3}


def test_session_exec_seconds_breach_helper_detects_ceiling(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 10.0)
    sidecar_main._session_exec_seconds = 10.5

    breach = sidecar_main._session_exec_seconds_breach()

    assert breach == {"reason": "exec_seconds", "limit": 10.0, "used": 10.5}


def test_session_exec_budget_error_detail_shape_differs_from_exec_response():
    detail = sidecar_main._session_exec_budget_error_detail(
        {"reason": "exec_count", "limit": 500, "used": 500}
    )

    assert detail["error_type"] == "session_budget_exceeded"
    assert "exit_code" not in detail
    assert "stdout" not in detail
    assert detail["reason"] == "exec_count"
    assert detail["limit"] == 500
    assert detail["used"] == 500


# ---------------------------------------------------------------------------
# Direct unit tests for the shared, lock-guarded reserve/record functions
# (replace the old single `_record_session_exec` helper, which bundled the
# count increment and the duration accumulation into one call made only
# from /exec -- now split so the count can be reserved atomically with the
# ceiling check, from any of the three routes, while duration is still only
# recorded after a call's own work finishes).
# ---------------------------------------------------------------------------


async def test_reserve_session_exec_slot_increments_count_and_allows_under_ceiling(monkeypatch):
    _reset_budget_state()
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 5)

    await sidecar_main._reserve_session_exec_slot_or_raise()
    await sidecar_main._reserve_session_exec_slot_or_raise()

    assert sidecar_main._session_exec_count == 2
    assert sidecar_main._session_budget_exceeded is None


async def test_reserve_session_exec_slot_raises_and_trips_sticky_flag_at_ceiling(monkeypatch):
    _reset_budget_state()
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 1)

    await sidecar_main._reserve_session_exec_slot_or_raise()

    try:
        await sidecar_main._reserve_session_exec_slot_or_raise()
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 403
        assert exc.detail["reason"] == "exec_count"

    assert sidecar_main._session_budget_exceeded is not None
    # The count is NOT incremented a second time by the breaching call --
    # only successful reservations increment it.
    assert sidecar_main._session_exec_count == 1


async def test_record_session_exec_duration_accumulates_seconds_only(monkeypatch):
    _reset_budget_state()
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 3600.0)

    await sidecar_main._record_session_exec_duration_or_raise(1.5)
    await sidecar_main._record_session_exec_duration_or_raise(2.25)

    assert sidecar_main._session_exec_seconds == 3.75
    # Duration recording never touches the count -- that's reserved
    # separately by _reserve_session_exec_slot_or_raise.
    assert sidecar_main._session_exec_count == 0


async def test_record_session_exec_duration_raises_and_trips_sticky_flag_at_ceiling(monkeypatch):
    _reset_budget_state()
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 1.0)

    try:
        await sidecar_main._record_session_exec_duration_or_raise(1.5)
        assert False, "expected HTTPException"
    except HTTPException as exc:
        assert exc.status_code == 403
        assert exc.detail["reason"] == "exec_seconds"

    assert sidecar_main._session_budget_exceeded is not None


# ---------------------------------------------------------------------------
# Cross-route coverage (the CRITICAL security-review findings): a session
# that breaches its budget via /exec must also be blocked -- without doing
# any of that route's own work -- on /interpreter/exec and /process/start.
# ---------------------------------------------------------------------------


def test_budget_exceeded_via_exec_blocks_interpreter_exec_without_spawning(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 1)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 0.0)
    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox_factory())

    import sidecar_interpreter

    spawn_calls = []

    async def _tracking_get_or_spawn_interpreter_locked():
        spawn_calls.append(1)
        raise AssertionError("interpreter must never be spawned once budget is exceeded")

    monkeypatch.setattr(
        sidecar_interpreter, "_get_or_spawn_interpreter_locked", _tracking_get_or_spawn_interpreter_locked
    )

    client = _client()

    first = client.post("/exec", json={"command": "echo 1", "timeout": 5}, headers=_headers())
    assert first.status_code == 200

    second = client.post("/exec", json={"command": "echo 2", "timeout": 5}, headers=_headers())
    assert second.status_code == 403  # budget tripped here, sticky flag now set

    interp_response = client.post(
        "/interpreter/exec", json={"code": "1 + 1"}, headers=_headers()
    )

    assert interp_response.status_code == 403
    body = interp_response.json()["detail"]
    assert body["error_type"] == "session_budget_exceeded"
    assert spawn_calls == []  # the interpreter was never even attempted


def _enable_lsp(monkeypatch, tmp_path):
    """Same bypass technique _bypass_nsenter_for_interpreter uses, plus
    pointing the LSP server command map at the fake driver script
    test_sidecar_lsp.py defines -- these tests care about the exec-budget
    wiring, not real pyright/typescript-language-server behavior."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "BOXKITE_LSP_ENABLED", True)
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 1)
    monkeypatch.setattr(
        sidecar_main, "build_k8s_exec_command", lambda pid, command: ["sh", "-c", command]
    )
    script_path = _write_fake_driver(tmp_path)
    fake_argv = [sys.executable, script_path]
    monkeypatch.setattr(
        sidecar_lsp, "_LSP_SERVER_COMMANDS", {"python": fake_argv, "typescript": fake_argv}
    )


def test_budget_exceeded_via_exec_blocks_lsp_start_without_spawning(monkeypatch, tmp_path):
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 1)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 0.0)
    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox_factory())
    _enable_lsp(monkeypatch, tmp_path)

    spawn_calls = []

    async def _tracking_spawn(language, lsp_id):
        spawn_calls.append(language)
        raise AssertionError("LSP server must never be spawned once budget is exceeded")

    monkeypatch.setattr(sidecar_lsp, "_spawn_lsp_server", _tracking_spawn)

    client = _client()

    first = client.post("/exec", json={"command": "echo 1", "timeout": 5}, headers=_headers())
    assert first.status_code == 200

    second = client.post("/exec", json={"command": "echo 2", "timeout": 5}, headers=_headers())
    assert second.status_code == 403  # budget tripped here, sticky flag now set

    lsp_response = client.post("/lsp/start", json={"language": "python"}, headers=_headers())

    assert lsp_response.status_code == 403
    body = lsp_response.json()["detail"]
    assert body["error_type"] == "session_budget_exceeded"
    assert spawn_calls == []  # the LSP server was never even attempted


def test_budget_exceeded_via_exec_blocks_process_start_without_spawning(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 1)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 0.0)
    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox_factory())

    spawn_calls = []

    async def _tracking_spawn(command, *, expose_network=False):
        spawn_calls.append(command)
        raise AssertionError("process must never be spawned once budget is exceeded")

    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _tracking_spawn)

    client = _client()

    first = client.post("/exec", json={"command": "echo 1", "timeout": 5}, headers=_headers())
    assert first.status_code == 200

    second = client.post("/exec", json={"command": "echo 2", "timeout": 5}, headers=_headers())
    assert second.status_code == 403  # budget tripped here, sticky flag now set

    process_response = client.post(
        "/process/start",
        json={"command": "sleep 1", "max_runtime_seconds": 5},
        headers=_headers(),
    )

    assert process_response.status_code == 403
    body = process_response.json()["detail"]
    assert body["error_type"] == "session_budget_exceeded"
    assert spawn_calls == []  # the process was never even attempted
    assert sidecar_main._process_registry == {}


# ---------------------------------------------------------------------------
# Usage attribution (the other half of the CRITICAL findings): looping via
# the interpreter or via background processes must actually consume budget,
# not just get blocked once the budget is already gone.
# ---------------------------------------------------------------------------


def test_interpreter_exec_calls_count_toward_the_shared_exec_count_budget(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 2)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 3600.0)
    _bypass_nsenter_for_interpreter(monkeypatch)
    _disable_periodic_sync(monkeypatch)

    with TestClient(sidecar_main.app) as client:
        first = client.post("/interpreter/exec", json={"code": "1 + 1"}, headers=_headers())
        assert first.status_code == 200
        assert sidecar_main._session_exec_count == 1

        second = client.post("/interpreter/exec", json={"code": "2 + 2"}, headers=_headers())
        assert second.status_code == 200
        assert sidecar_main._session_exec_count == 2

        third = client.post("/interpreter/exec", json={"code": "3 + 3"}, headers=_headers())
        assert third.status_code == 403
        body = third.json()["detail"]
        assert body["error_type"] == "session_budget_exceeded"
        assert body["reason"] == "exec_count"


def test_interpreter_exec_duration_counts_toward_the_shared_exec_seconds_budget(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 0)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 0.05)
    _bypass_nsenter_for_interpreter(monkeypatch)
    _disable_periodic_sync(monkeypatch)

    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/interpreter/exec",
            json={"code": "import time; time.sleep(0.15)"},
            headers=_headers(),
        )

    assert response.status_code == 403
    body = response.json()["detail"]
    assert body["error_type"] == "session_budget_exceeded"
    assert body["reason"] == "exec_seconds"
    assert body["used"] > 0.05


def test_process_start_calls_count_toward_the_shared_exec_count_budget(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 1)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 0.0)
    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _fake_spawn_background_process)
    _disable_periodic_sync(monkeypatch)

    with TestClient(sidecar_main.app) as client:
        first = client.post(
            "/process/start",
            json={"command": "sleep 0.2", "max_runtime_seconds": 5},
            headers=_headers(),
        )
        assert first.status_code == 201
        assert sidecar_main._session_exec_count == 1

        second = client.post(
            "/process/start",
            json={"command": "sleep 0.2", "max_runtime_seconds": 5},
            headers=_headers(),
        )
        assert second.status_code == 403
        body = second.json()["detail"]
        assert body["error_type"] == "session_budget_exceeded"
        assert body["reason"] == "exec_count"
        # The breaching 2nd call tears the session down (kills every live
        # process, including the one the 1st call started), same as an
        # /exec breach would -- the registry ends up empty, not because
        # the spawn call count is wrong.
        assert sidecar_main._process_registry == {}


def test_lsp_start_calls_count_toward_the_shared_exec_count_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 2)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 3600.0)
    _enable_lsp(monkeypatch, tmp_path)
    _disable_periodic_sync(monkeypatch)

    with TestClient(sidecar_main.app) as client:
        first = client.post("/lsp/start", json={"language": "python"}, headers=_headers())
        assert first.status_code == 201
        assert sidecar_main._session_exec_count == 1

        second = client.post("/lsp/start", json={"language": "typescript"}, headers=_headers())
        assert second.status_code == 201
        assert sidecar_main._session_exec_count == 2

        third = client.post("/exec", json={"command": "echo hi"}, headers=_headers())
        assert third.status_code == 403
        body = third.json()["detail"]
        assert body["error_type"] == "session_budget_exceeded"
        assert body["reason"] == "exec_count"


def test_lsp_completion_calls_count_toward_the_shared_exec_count_budget(monkeypatch, tmp_path):
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 2)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 3600.0)
    _enable_lsp(monkeypatch, tmp_path)
    _disable_periodic_sync(monkeypatch)

    with TestClient(sidecar_main.app) as client:
        start_response = client.post("/lsp/start", json={"language": "python"}, headers=_headers())
        assert start_response.status_code == 201
        lsp_id = start_response.json()["lsp_id"]
        assert sidecar_main._session_exec_count == 1

        client.post(
            f"/lsp/{lsp_id}/open",
            json={"path": "a.py", "content": "import os\nos.pat"},
            headers=_headers(),
        )
        # /lsp/{id}/open is NOT exec-budget-checked (a document-sync
        # notification with no RPC response awaited, same classification
        # /process/input and /interpreter/reset already have).
        assert sidecar_main._session_exec_count == 1

        completion_response = client.post(
            f"/lsp/{lsp_id}/completion",
            json={"path": "a.py", "line": 1, "character": 6},
            headers=_headers(),
        )
        assert completion_response.status_code == 200
        assert sidecar_main._session_exec_count == 2

        third = client.post(
            f"/lsp/{lsp_id}/completion",
            json={"path": "a.py", "line": 1, "character": 6},
            headers=_headers(),
        )
        assert third.status_code == 403
        body = third.json()["detail"]
        assert body["error_type"] == "session_budget_exceeded"
        assert body["reason"] == "exec_count"


async def test_lsp_start_duration_breach_kills_the_just_spawned_server_not_leaks_it(
    monkeypatch, tmp_path
):
    """Regression test: a successfully-spawned LSP server must be
    registered BEFORE its own call's duration is recorded, not after --
    otherwise a duration breach on the very call that spawned it tears the
    session down via _kill_all_lsp_servers() without that process ever
    having been visible to the registry, leaking a real, still-running
    subprocess (the registry ends up empty either way -- either because
    teardown removed it after killing it, or because it was never added at
    all -- so asserting on registry emptiness alone would NOT catch this;
    the real assertion is that the actual OS process was killed).
    """
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 0)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 0.0001)
    _enable_lsp(monkeypatch, tmp_path)
    _disable_periodic_sync(monkeypatch)

    spawned_handles = []
    real_spawn = sidecar_lsp._spawn_lsp_server

    async def _tracking_spawn(language, lsp_id):
        handle = await real_spawn(language, lsp_id)
        spawned_handles.append(handle)
        return handle

    monkeypatch.setattr(sidecar_lsp, "_spawn_lsp_server", _tracking_spawn)

    with TestClient(sidecar_main.app) as client:
        response = client.post("/lsp/start", json={"language": "python"}, headers=_headers())

    assert response.status_code == 403
    body = response.json()["detail"]
    assert body["error_type"] == "session_budget_exceeded"
    assert body["reason"] == "exec_seconds"

    assert len(spawned_handles) == 1
    handle = spawned_handles[0]
    # Give the async kill (SIGKILL + proc.wait()) a moment to complete --
    # the breach's teardown runs synchronously inside the request, but
    # proc.wait() resolving is a real OS-level event.
    await asyncio.wait_for(handle.proc.wait(), timeout=5)
    assert handle.proc.returncode is not None  # the real subprocess was actually killed
    assert sidecar_main._lsp_registry == {}


# ---------------------------------------------------------------------------
# The MEDIUM TOCTOU fix: the exec-count precheck and its increment are now
# one lock-guarded atomic step, so N genuinely concurrent callers near the
# ceiling cannot all pass the precheck before any of them records its usage.
# Before this fix, the precheck (`_session_exec_count_breach`) and the
# increment (the old `_record_session_exec`) were separated by an unguarded
# `await` -- exactly the shape that lets concurrent callers race past the
# ceiling. This exercises the real, currently-shipping
# `_reserve_session_exec_slot_or_raise` under genuine concurrency (via
# asyncio.gather, so calls actually contend for the lock instead of running
# sequentially) and asserts the ceiling is never overshot.
# ---------------------------------------------------------------------------


async def test_reserve_session_exec_slot_lock_prevents_ceiling_overshoot_under_concurrency(monkeypatch):
    _reset_budget_state()
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 5)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 0.0)

    concurrency = 20
    results = await asyncio.gather(
        *[sidecar_main._reserve_session_exec_slot_or_raise() for _ in range(concurrency)],
        return_exceptions=True,
    )

    successes = [r for r in results if r is None]
    rejections = [r for r in results if isinstance(r, HTTPException)]
    unexpected = [r for r in results if r is not None and not isinstance(r, HTTPException)]

    assert unexpected == []
    assert len(successes) == 5
    assert len(rejections) == concurrency - 5
    # The whole point of the fix: the count lands EXACTLY at the ceiling,
    # never above it, no matter how many concurrent callers raced for it.
    assert sidecar_main._session_exec_count == 5
    for rejection in rejections:
        assert rejection.status_code == 403
        assert rejection.detail["error_type"] == "session_budget_exceeded"
        assert rejection.detail["reason"] == "exec_count"
