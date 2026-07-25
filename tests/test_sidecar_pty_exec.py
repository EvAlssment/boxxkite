"""Tests for the sidecar's agent-callable PTY endpoint (POST /pty-exec,
docs/AGENT-PTY-DESIGN.md).

Mirrors test_sidecar_pty.py's pattern: monkeypatch build_pty_command to
spawn a real local command directly (bypassing nsenter/docker-exec, which
have no sandbox container to attach to in this test environment) so the
actual PTY allocation, exec, and output-capture mechanics are exercised
for real, against a real process -- not mocked away.

Covers:
- 404 when BOXKITE_AGENT_PTY_ENABLED is off (the default).
- Requires the same sidecar auth as every other route once enabled.
- A real command's output, printed via a real pseudo-terminal (so
  isatty() is true), comes back in the response.
- input_bytes (base64) is written to the process's stdin.
- A command that runs past timeout_seconds is killed and timed_out=True.
- exec_argv is never run through a shell -- shell metacharacters in the
  command are inert (passed as literal argv, not interpreted).
"""

import base64

import main as sidecar_main
from fastapi.testclient import TestClient


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def _enable(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(sidecar_main, "BOXKITE_AGENT_PTY_ENABLED", True)


def _use_real_shell(monkeypatch):
    """build_pty_command normally nsenters/docker-execs into a sandbox
    container that doesn't exist in this test environment -- swap in a
    version that execs the caller's argv directly via a real local shell,
    same substitution test_sidecar_pty.py's own tests use."""
    monkeypatch.setattr(
        sidecar_main,
        "build_pty_command",
        lambda argv=None: argv or ["/bin/bash", "--norc", "--noprofile"],
    )


def test_pty_exec_404s_when_disabled_by_default(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    client = _client()

    response = client.post(
        "/pty-exec", json={"command": "echo hi"}, headers=_auth_headers()
    )

    assert response.status_code == 404


def test_pty_exec_requires_auth_like_every_other_route(monkeypatch):
    _enable(monkeypatch)
    client = _client()

    response = client.post("/pty-exec", json={"command": "echo hi"})

    assert response.status_code == 401


def test_pty_exec_runs_a_real_command_behind_a_real_pty(monkeypatch):
    _enable(monkeypatch)
    _use_real_shell(monkeypatch)
    client = _client()

    marker = "hello_from_pty_exec_test_98765"
    response = client.post(
        "/pty-exec",
        json={"command": f"echo {marker}", "timeout_seconds": 5},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert marker in body["output"]
    assert body["exit_code"] == 0
    assert body["timed_out"] is False


def test_pty_exec_writes_input_bytes_to_stdin(monkeypatch):
    _enable(monkeypatch)
    _use_real_shell(monkeypatch)
    client = _client()

    input_bytes = base64.b64encode(b"typed input\n").decode()
    response = client.post(
        "/pty-exec",
        json={"command": "cat", "input_bytes": input_bytes, "timeout_seconds": 2},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert "typed input" in response.json()["output"]


def test_pty_exec_times_out_a_long_running_command(monkeypatch):
    _enable(monkeypatch)
    _use_real_shell(monkeypatch)
    client = _client()

    response = client.post(
        "/pty-exec",
        json={"command": "sleep 30", "timeout_seconds": 0.5},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert response.json()["timed_out"] is True


def test_pty_exec_command_is_never_shell_interpreted(monkeypatch):
    """Shell metacharacters in `command` are literal argv, not shell syntax
    -- `;`/`&&`/`$(...)` must not chain a second command."""
    _enable(monkeypatch)
    _use_real_shell(monkeypatch)
    client = _client()

    response = client.post(
        "/pty-exec",
        json={"command": "echo one; echo two", "timeout_seconds": 5},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    output = response.json()["output"]
    # shlex.split("echo one; echo two") -> ["echo", "one;", "echo", "two"]
    # -- `echo` with those literal args, never two separate commands.
    assert "one;" in output
    assert "two" in output


def test_pty_exec_rejects_empty_command(monkeypatch):
    _enable(monkeypatch)
    client = _client()

    response = client.post(
        "/pty-exec", json={"command": "   "}, headers=_auth_headers()
    )

    assert response.status_code == 400


def test_pty_exec_rejects_invalid_base64_input(monkeypatch):
    _enable(monkeypatch)
    client = _client()

    response = client.post(
        "/pty-exec",
        json={"command": "echo hi", "input_bytes": "not-valid-base64!!!"},
        headers=_auth_headers(),
    )

    assert response.status_code == 400
