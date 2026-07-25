"""Tests for the sidecar's persistent Node.js interpreter (/node-interpreter/*).

Mirrors tests/test_sidecar_interpreter.py's coverage for the Python
interpreter (see docs/NODE-INTERPRETER-DESIGN.md), plus the
enabled/disabled gating tests tests/test_sidecar_pty_exec.py uses for
BOXKITE_AGENT_PTY_ENABLED -- this feature is gated the same way:

- 404 when BOXKITE_NODE_INTERPRETER_ENABLED is off (the default).
- Requires the same sidecar auth as every other route once enabled.
- State (variables/functions) persists across separate calls.
- stdout and the last expression's value (via util.inspect) are returned.
- Errors are reported without killing the interpreter or losing state.
- /node-interpreter/reset kills the interpreter; the next call starts fresh.
- /node-interpreter/status reports running/idle_seconds honestly.
- Idle timeout kills the interpreter (NODE_INTERPRETER_IDLE_TIMEOUT_SECONDS).
- Output is capped (NODE_INTERPRETER_MAX_OUTPUT_BYTES) with truncated=True.
- /configure kills any live Node interpreter before wiping session state,
  the same cross-tenant-leak mitigation the Python interpreter already has.

These tests bypass exec_in_sandbox's nsenter/docker-exec namespace-entry
step (there is no real sandbox container in this test environment) by
monkeypatching get_sandbox_pid/build_k8s_exec_command to run a plain local
`sh -c <command>` directly -- same technique test_sidecar_interpreter.py and
test_sidecar_pty.py use.
"""

import os
import shutil

import main as sidecar_main
from fastapi.testclient import TestClient


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def _enable(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "BOXKITE_NODE_INTERPRETER_ENABLED", True)
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 1)
    monkeypatch.setattr(
        sidecar_main, "build_k8s_exec_command", lambda pid, command: ["sh", "-c", command]
    )
    # SAFE_EXEC_ENV's PATH is deliberately restricted to the paths `node`
    # actually lives at *inside the sandbox container* (built by
    # deploy/sandbox.Dockerfile's `apk add nodejs-22`, which installs to
    # /usr/bin). This test bypasses nsenter/docker-exec entirely (there is
    # no sandbox container here) and runs a plain local shell instead, so it
    # needs `node` to be reachable via whatever PATH this dev/CI machine
    # actually has it on -- extend SAFE_EXEC_ENV's PATH for the test process
    # only, never touching the real constant shipped to production.
    node_path = shutil.which("node")
    if node_path:
        node_dir = os.path.dirname(node_path)
        extended_env = dict(sidecar_main.SAFE_EXEC_ENV)
        extended_env["PATH"] = f"{extended_env['PATH']}{os.pathsep}{node_dir}"
        monkeypatch.setattr(sidecar_main, "SAFE_EXEC_ENV", extended_env)


def test_node_interpreter_exec_404s_when_disabled_by_default(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/node-interpreter/exec", json={"code": "1"}, headers=_auth_headers()
        )
        assert response.status_code == 404


def test_node_interpreter_reset_404s_when_disabled_by_default(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    with TestClient(sidecar_main.app) as client:
        response = client.post("/node-interpreter/reset", headers=_auth_headers())
        assert response.status_code == 404


def test_node_interpreter_status_404s_when_disabled_by_default(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    with TestClient(sidecar_main.app) as client:
        response = client.get("/node-interpreter/status", headers=_auth_headers())
        assert response.status_code == 404


def test_node_interpreter_exec_requires_auth_like_every_other_route(monkeypatch):
    _enable(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post("/node-interpreter/exec", json={"code": "1"})
        assert response.status_code == 401


def test_node_interpreter_persists_variables_across_calls(monkeypatch):
    _enable(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        r1 = client.post(
            "/node-interpreter/exec", json={"code": "var x = 40"}, headers=_auth_headers()
        )
        assert r1.status_code == 200
        assert r1.json()["error"] is None

        r2 = client.post(
            "/node-interpreter/exec", json={"code": "x + 2"}, headers=_auth_headers()
        )
        assert r2.status_code == 200
        body = r2.json()
        assert body["result"] == "42"
        assert body["error"] is None


def test_node_interpreter_persists_let_and_function_declarations(monkeypatch):
    """Top-level `let`/`function` also persist across calls -- both attach
    to the interpreter process's global lexical scope via indirect eval,
    the same way they'd persist across separate lines typed into a real
    Node REPL or browser devtools console."""
    _enable(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        client.post(
            "/node-interpreter/exec",
            json={"code": "let greeting = 'hi'; function shout(s) { return s.toUpperCase(); }"},
            headers=_auth_headers(),
        )

        response = client.post(
            "/node-interpreter/exec",
            json={"code": "shout(greeting)"},
            headers=_auth_headers(),
        )
        body = response.json()
        assert body["result"] == "'HI'"
        assert body["error"] is None


def test_node_interpreter_returns_stdout_and_last_expression_value(monkeypatch):
    _enable(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/node-interpreter/exec",
            json={"code": "console.log('hello'); 1 + 1"},
            headers=_auth_headers(),
        )
        body = response.json()
        assert body["stdout"] == "hello\n"
        assert body["result"] == "2"
        assert body["error"] is None


def test_node_interpreter_reports_errors_without_losing_state(monkeypatch):
    _enable(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        client.post(
            "/node-interpreter/exec", json={"code": "var y = 7"}, headers=_auth_headers()
        )

        error_response = client.post(
            "/node-interpreter/exec",
            json={"code": "throw new Error('boom')"},
            headers=_auth_headers(),
        )
        error_body = error_response.json()
        assert error_body["error"] is not None
        assert "boom" in error_body["error"]

        follow_up = client.post(
            "/node-interpreter/exec", json={"code": "y"}, headers=_auth_headers()
        )
        assert follow_up.json()["result"] == "7"


def test_node_interpreter_exec_rejects_empty_code(monkeypatch):
    _enable(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/node-interpreter/exec", json={"code": "   "}, headers=_auth_headers()
        )
        assert response.status_code == 400


def test_node_interpreter_reset_clears_state(monkeypatch):
    _enable(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        client.post(
            "/node-interpreter/exec", json={"code": "var z = 1"}, headers=_auth_headers()
        )

        reset_response = client.post("/node-interpreter/reset", headers=_auth_headers())
        assert reset_response.status_code == 200
        assert reset_response.json() == {"status": "reset"}

        after_reset = client.post(
            "/node-interpreter/exec", json={"code": "typeof z"}, headers=_auth_headers()
        )
        after_body = after_reset.json()
        assert after_body["result"] == "'undefined'"
        assert after_body["error"] is None


def test_node_interpreter_status_reports_not_running_before_first_call(monkeypatch):
    _enable(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.get("/node-interpreter/status", headers=_auth_headers())
        assert response.json() == {"running": False, "started_at": None, "idle_seconds": None}


def test_node_interpreter_status_reports_running_after_a_call(monkeypatch):
    _enable(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        client.post("/node-interpreter/exec", json={"code": "1"}, headers=_auth_headers())

        response = client.get("/node-interpreter/status", headers=_auth_headers())
        body = response.json()
        assert body["running"] is True
        assert body["started_at"] is not None
        assert body["idle_seconds"] is not None


async def test_node_interpreter_idle_reaper_kills_process_past_timeout(monkeypatch):
    """Drives _get_or_spawn_node_interpreter_locked/_reap_idle_node_interpreter
    directly (no TestClient/HTTP) so this coroutine and the subprocess it
    awaits share one event loop -- same reasoning
    test_interpreter_idle_reaper_kills_process_past_timeout gives."""
    _enable(monkeypatch)
    monkeypatch.setattr(sidecar_main, "NODE_INTERPRETER_IDLE_TIMEOUT_SECONDS", 0)

    async with sidecar_main._get_node_interpreter_lock():
        handle = await sidecar_main._get_or_spawn_node_interpreter_locked()
    assert handle.proc.returncode is None

    await sidecar_main._reap_idle_node_interpreter()

    assert sidecar_main._node_interpreter_handle is None
    assert handle.proc.returncode is not None


def test_node_interpreter_output_is_truncated_past_the_byte_cap(monkeypatch):
    _enable(monkeypatch)
    monkeypatch.setattr(sidecar_main, "NODE_INTERPRETER_MAX_OUTPUT_BYTES", 16)

    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/node-interpreter/exec",
            json={"code": "console.log('x'.repeat(1000))"},
            headers=_auth_headers(),
        )
        body = response.json()
        assert body["truncated"] is True
        assert len(body["stdout"].encode("utf-8")) <= 16


def test_configure_kills_live_node_interpreter_before_wiping_session(monkeypatch, tmp_path):
    """Regression test mirroring
    test_configure_kills_live_interpreter_before_wiping_session for the Node
    interpreter: a recycled pod must never hand a new tenant a still-live
    Node interpreter (and its state) left over from the previous tenant."""
    _enable(monkeypatch)

    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(sidecar_main, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(sidecar_main, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(sidecar_main, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(sidecar_main, "TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(sidecar_main.os, "chown", lambda *args, **kwargs: None)

    with TestClient(sidecar_main.app) as client:
        client.post(
            "/node-interpreter/exec",
            json={"code": "var tenantASecret = 'leaked-if-still-here'"},
            headers=_auth_headers(),
        )
        assert (
            client.get("/node-interpreter/status", headers=_auth_headers()).json()["running"]
            is True
        )

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

        status_after = client.get("/node-interpreter/status", headers=_auth_headers()).json()
        assert status_after["running"] is False

        next_tenant = client.post(
            "/node-interpreter/exec",
            json={"code": "typeof tenantASecret"},
            headers=_auth_headers(),
        )
        next_tenant_body = next_tenant.json()
        assert next_tenant_body["result"] == "'undefined'"
        assert next_tenant_body["error"] is None


def test_spawn_node_interpreter_fails_when_sandbox_process_is_missing(monkeypatch):
    """K8s mode: if get_sandbox_pid() can't find the sandbox process,
    /node-interpreter/exec must fail loudly (502), not hang or silently no-op."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "BOXKITE_NODE_INTERPRETER_ENABLED", True)
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: None)

    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/node-interpreter/exec", json={"code": "1"}, headers=_auth_headers()
        )
        assert response.status_code == 502
