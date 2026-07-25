"""Tests for High #4: /exec must not accept caller-supplied environment variables.

An earlier version of ExecRequest had an `env: Optional[Dict[str, str]]`
field that was injected verbatim into the exec'd sandbox process's
environment — a real credential-injection path directly reachable by
agent-visible /exec calls, with no real call site in this codebase ever
using it (SandboxManager.execute() never accepted or forwarded an env
kwarg). It was removed rather than hardened with exact-value scrubbing,
since there was no legitimate use to preserve.
"""

import main as sidecar_main


def test_exec_request_has_no_env_field():
    assert "env" not in sidecar_main.ExecRequest.model_fields


def test_exec_request_ignores_unexpected_env_field(monkeypatch):
    """Even if a caller sends `env` (e.g. an old client), it must not survive
    into the validated request object or reach the subprocess."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")

    captured_env = {}

    async def _fake_create_subprocess_exec(*cmd, env=None, **kwargs):
        captured_env.update(env or {})

        class _FakeProc:
            returncode = 0

            async def communicate(self):
                return b"", b""

            def kill(self):
                pass

            async def wait(self):
                return None

        return _FakeProc()

    monkeypatch.setattr(sidecar_main.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "compose")
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 4242)

    from fastapi.testclient import TestClient

    client = TestClient(sidecar_main.app)
    response = client.post(
        "/exec",
        json={
            "command": "echo hi",
            "env": {"INJECTED_SECRET": "should-not-reach-subprocess"},
        },
        headers={sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"},
    )

    assert response.status_code == 200
    assert "INJECTED_SECRET" not in captured_env
