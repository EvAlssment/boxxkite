"""Tests for SandboxManager.lsp_start/lsp_open/lsp_completion/lsp_stop.

Mirrors tests/test_manager_node_interpreter.py's pattern for the LSP
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


async def test_lsp_start_posts_language_to_sidecar():
    manager, fake_client = _manager_with_stubbed_sidecar({"lsp_id": "lsp_abc123"})

    result = await manager.lsp_start(session_id="session-1", language="python")

    assert result == {"lsp_id": "lsp_abc123"}
    assert fake_client.post_calls == [("/lsp/start", {"language": "python"})]


async def test_lsp_open_posts_path_and_content_to_sidecar():
    manager, fake_client = _manager_with_stubbed_sidecar({"status": "opened"})

    result = await manager.lsp_open(
        session_id="session-1", lsp_id="lsp_abc123", path="a.py", content="import os"
    )

    assert result == {"status": "opened"}
    assert fake_client.post_calls == [
        ("/lsp/lsp_abc123/open", {"path": "a.py", "content": "import os"})
    ]


async def test_lsp_completion_posts_path_line_and_character_to_sidecar():
    manager, fake_client = _manager_with_stubbed_sidecar(
        {"items": [{"label": "path", "kind": 6}]}
    )

    result = await manager.lsp_completion(
        session_id="session-1", lsp_id="lsp_abc123", path="a.py", line=1, character=6
    )

    assert result == {"items": [{"label": "path", "kind": 6}]}
    assert fake_client.post_calls == [
        ("/lsp/lsp_abc123/completion", {"path": "a.py", "line": 1, "character": 6})
    ]


async def test_lsp_stop_posts_to_sidecar_with_no_body():
    manager, fake_client = _manager_with_stubbed_sidecar({"status": "stopped"})

    result = await manager.lsp_stop(session_id="session-1", lsp_id="lsp_abc123")

    assert result == {"status": "stopped"}
    assert fake_client.post_calls == [("/lsp/lsp_abc123/stop", {})]
