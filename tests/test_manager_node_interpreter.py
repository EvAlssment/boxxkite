"""Tests for SandboxManager.node_interpreter_exec/node_interpreter_reset.

Mirrors tests/test_manager_interpreter.py's pattern for the Node.js
counterpart: resolve the session, get the pod's HTTP client, POST to the
sidecar, return the parsed JSON body.
"""

import pytest

from boxkite.manager import SandboxManager

pytestmark = pytest.mark.pr


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeHttpClient:
    def __init__(self, payload: dict):
        self._payload = payload
        self.post_calls: list[tuple[str, dict]] = []

    async def post(self, path: str, json: dict | None = None) -> _FakeResponse:
        self.post_calls.append((path, json or {}))
        return _FakeResponse(self._payload)


def _manager_with_stubbed_sidecar(payload: dict) -> tuple[SandboxManager, _FakeHttpClient]:
    manager = SandboxManager()
    fake_client = _FakeHttpClient(payload)

    async def fake_resolve_session(session_id: str):
        return ("sandbox-pod", "10.8.0.80")

    manager._resolve_session = fake_resolve_session
    manager._get_http_client = lambda *args, **kwargs: fake_client
    return manager, fake_client


async def test_node_interpreter_exec_posts_code_and_timeout_to_sidecar():
    manager, fake_client = _manager_with_stubbed_sidecar(
        {"stdout": "", "result": "42", "error": None, "truncated": False}
    )

    result = await manager.node_interpreter_exec(
        session_id="session-1", code="40 + 2", timeout=15
    )

    assert result == {"stdout": "", "result": "42", "error": None, "truncated": False}
    assert fake_client.post_calls == [
        ("/node-interpreter/exec", {"code": "40 + 2", "timeout": 15})
    ]


async def test_node_interpreter_exec_defaults_timeout_to_30_seconds():
    manager, fake_client = _manager_with_stubbed_sidecar(
        {"stdout": "", "result": None, "error": None, "truncated": False}
    )

    await manager.node_interpreter_exec(session_id="session-1", code="1")

    assert fake_client.post_calls == [("/node-interpreter/exec", {"code": "1", "timeout": 30})]


async def test_node_interpreter_reset_posts_to_sidecar_with_no_body():
    manager, fake_client = _manager_with_stubbed_sidecar({"status": "reset"})

    result = await manager.node_interpreter_reset(session_id="session-1")

    assert result == {"status": "reset"}
    assert fake_client.post_calls == [("/node-interpreter/reset", {})]
