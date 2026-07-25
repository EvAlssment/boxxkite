"""Tests for the sidecar's persistent Python interpreter (/interpreter/*).

Covers the scope from docs/DAYTONA-COMPARISON.md's "Multi-language stateful
code execution" gap: a kept-alive Python process per session that keeps
variable state across calls, distinct from /exec's always-fresh
`python3 -c ...` subprocess. Specifically:

- State (variables) survives across separate /interpreter/exec calls.
- stdout and the last expression's repr are both returned per call.
- Errors are reported without killing the interpreter or losing state.
- /interpreter/reset kills the interpreter; the next call starts fresh.
- /interpreter/status reports running/idle_seconds honestly.
- Idle timeout kills the interpreter (INTERPRETER_IDLE_TIMEOUT_SECONDS).
- Output is capped (INTERPRETER_MAX_OUTPUT_BYTES) with truncated=True.
- A single call's stdout between asyncio's default 64KB StreamReader
  line-buffer limit and INTERPRETER_MAX_OUTPUT_BYTES's default 256KB cap
  succeeds untruncated, instead of 500ing on LimitOverrunError.
- /configure (the warm-pool recycle path) kills any live interpreter before
  wiping session state, closing the cross-tenant leak
  docs/PROCESS-SESSIONS-DESIGN.md's §2(b) flags for kept-alive processes.

These tests bypass exec_in_sandbox's nsenter/docker-exec namespace-entry
step (there is no real sandbox container in this test environment) by
monkeypatching get_sandbox_pid/build_k8s_exec_command to run a plain local
`sh -c <command>` directly -- same technique tests/test_sidecar_pty.py uses
for build_pty_command.
"""

import main as sidecar_main
from fastapi.testclient import TestClient


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def _bypass_nsenter(monkeypatch):
    """Route the interpreter's spawn command straight to a local shell."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 1)
    monkeypatch.setattr(
        sidecar_main, "build_k8s_exec_command", lambda pid, command: ["sh", "-c", command]
    )


def test_interpreter_persists_variables_across_calls(monkeypatch):
    _bypass_nsenter(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        r1 = client.post("/interpreter/exec", json={"code": "x = 40"}, headers=_auth_headers())
        assert r1.status_code == 200
        assert r1.json()["error"] is None

        r2 = client.post("/interpreter/exec", json={"code": "x + 2"}, headers=_auth_headers())
        assert r2.status_code == 200
        body = r2.json()
        assert body["result"] == "42"
        assert body["error"] is None


def test_interpreter_returns_stdout_and_last_expression_repr(monkeypatch):
    _bypass_nsenter(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/interpreter/exec",
            json={"code": "print('hello')\n1 + 1"},
            headers=_auth_headers(),
        )
        body = response.json()
        assert body["stdout"] == "hello\n"
        assert body["result"] == "2"
        assert body["error"] is None


def test_interpreter_reports_errors_without_losing_state(monkeypatch):
    _bypass_nsenter(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        client.post("/interpreter/exec", json={"code": "y = 7"}, headers=_auth_headers())

        error_response = client.post(
            "/interpreter/exec", json={"code": "1 / 0"}, headers=_auth_headers()
        )
        error_body = error_response.json()
        assert error_body["error"] is not None
        assert "ZeroDivisionError" in error_body["error"]

        follow_up = client.post("/interpreter/exec", json={"code": "y"}, headers=_auth_headers())
        assert follow_up.json()["result"] == "7"


def test_interpreter_exec_rejects_empty_code(monkeypatch):
    _bypass_nsenter(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post("/interpreter/exec", json={"code": "   "}, headers=_auth_headers())
        assert response.status_code == 400


def test_interpreter_reset_clears_state(monkeypatch):
    _bypass_nsenter(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        client.post("/interpreter/exec", json={"code": "z = 1"}, headers=_auth_headers())

        reset_response = client.post("/interpreter/reset", headers=_auth_headers())
        assert reset_response.status_code == 200
        assert reset_response.json() == {"status": "reset"}

        after_reset = client.post("/interpreter/exec", json={"code": "z"}, headers=_auth_headers())
        after_body = after_reset.json()
        assert after_body["result"] is None
        assert "NameError" in after_body["error"]


def test_interpreter_status_reports_not_running_before_first_call(monkeypatch):
    _bypass_nsenter(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.get("/interpreter/status", headers=_auth_headers())
        assert response.json() == {"running": False, "started_at": None, "idle_seconds": None}


def test_interpreter_status_reports_running_after_a_call(monkeypatch):
    _bypass_nsenter(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        client.post("/interpreter/exec", json={"code": "1"}, headers=_auth_headers())

        response = client.get("/interpreter/status", headers=_auth_headers())
        body = response.json()
        assert body["running"] is True
        assert body["started_at"] is not None
        assert body["idle_seconds"] is not None


async def test_interpreter_idle_reaper_kills_process_past_timeout(monkeypatch):
    """The periodic idle-reap sweep (same cadence as the sync loop) must
    kill a forgotten interpreter, not just /interpreter/reset.

    Drives _get_or_spawn_interpreter_locked/_reap_idle_interpreter directly
    (no TestClient/HTTP) so this coroutine and the subprocess it awaits
    share one event loop -- going through TestClient's own background-loop
    portal for a bare `asyncio.run()` call would attach the awaited
    subprocess future to a different loop than the one that created it.
    """
    _bypass_nsenter(monkeypatch)
    monkeypatch.setattr(sidecar_main, "INTERPRETER_IDLE_TIMEOUT_SECONDS", 0)

    async with sidecar_main._get_interpreter_lock():
        handle = await sidecar_main._get_or_spawn_interpreter_locked()
    assert handle.proc.returncode is None

    await sidecar_main._reap_idle_interpreter()

    assert sidecar_main._interpreter_handle is None
    assert handle.proc.returncode is not None


def test_interpreter_output_is_truncated_past_the_byte_cap(monkeypatch):
    _bypass_nsenter(monkeypatch)
    monkeypatch.setattr(sidecar_main, "INTERPRETER_MAX_OUTPUT_BYTES", 16)

    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/interpreter/exec",
            json={"code": "print('x' * 1000)"},
            headers=_auth_headers(),
        )
        body = response.json()
        assert body["truncated"] is True
        assert len(body["stdout"].encode("utf-8")) <= 16


def test_interpreter_handles_output_between_64kb_and_256kb(monkeypatch):
    """Regression test: asyncio.StreamReader.readline()'s default 64KB
    line-buffer limit is well below INTERPRETER_MAX_OUTPUT_BYTES's own
    default 256KB cap, so a call whose stdout falls in between used to
    raise LimitOverrunError inside the sidecar and 500 -- on exactly the
    large-output case INTERPRETER_MAX_OUTPUT_BYTES claims to support.
    """
    _bypass_nsenter(monkeypatch)

    output_bytes = 150 * 1024
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/interpreter/exec",
            json={"code": f"print('a' * {output_bytes})"},
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["error"] is None
        assert body["truncated"] is False
        assert body["stdout"] == "a" * output_bytes + "\n"


def test_configure_kills_live_interpreter_before_wiping_session(monkeypatch, tmp_path):
    """Regression test for the cross-tenant leak docs/PROCESS-SESSIONS-DESIGN.md
    §2(b) calls out for kept-alive processes: a recycled pod must never hand
    a new tenant a still-live interpreter (and its globals) left over from
    the previous tenant."""
    _bypass_nsenter(monkeypatch)

    # /configure also wipes real session directories and chowns them to the
    # sandbox UID -- neither is meaningful in this test environment (no real
    # sandbox container, and chown to an arbitrary UID requires root), so
    # point those paths at a scratch dir and no-op chown, matching the
    # pattern tests/test_sidecar_path_toctou.py uses for the same reason.
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(sidecar_main, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(sidecar_main, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(sidecar_main, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(sidecar_main, "TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(sidecar_main.os, "chown", lambda *args, **kwargs: None)

    with TestClient(sidecar_main.app) as client:
        client.post(
            "/interpreter/exec",
            json={"code": "tenant_a_secret = 'leaked-if-still-here'"},
            headers=_auth_headers(),
        )
        assert client.get("/interpreter/status", headers=_auth_headers()).json()["running"] is True

        configure_response = client.post(
            "/configure",
            json={
                "session_id": None,
                "organization_id": None,
                "work_item_id": None,
                "storage_prefix": None,
            },
            headers=_auth_headers(),
        )
        assert configure_response.status_code == 200

        status_after = client.get("/interpreter/status", headers=_auth_headers()).json()
        assert status_after["running"] is False

        # A fresh interpreter for the "next tenant" must not see the old globals.
        next_tenant = client.post(
            "/interpreter/exec", json={"code": "tenant_a_secret"}, headers=_auth_headers()
        )
        next_tenant_body = next_tenant.json()
        assert next_tenant_body["result"] is None
        assert "NameError" in next_tenant_body["error"]


def test_spawn_interpreter_fails_when_sandbox_process_is_missing(monkeypatch):
    """K8s mode: if get_sandbox_pid() can't find the sandbox process,
    /interpreter/exec must fail loudly (502), not hang or silently no-op."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: None)

    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/interpreter/exec", json={"code": "1"}, headers=_auth_headers()
        )
        assert response.status_code == 502
