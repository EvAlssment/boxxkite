"""An actual end-to-end exercise of the real BoxkiteClient + a real adapter
against `create_handoff_sandbox` -- not the fully-faked client used by
test_orchestrator.py's unit tests. This is the closest thing to "run it for
real" available in this environment: no live control-plane, sidecar, or
Kubernetes cluster is reachable here, so this substitutes an
`httpx.MockTransport` for HTTP and a fake `ws_connect` for the takeover
websocket -- both officially-supported BoxkiteClient test-injection points
(see client.py's own docstring: "`transport` is exposed on both
constructors purely for testing"), not a private/undocumented hack.

What this proves that the unit tests don't: the real `BoxkiteClient` builds
correct request URLs/JSON bodies/Authorization headers, a real adapter
(`ClaudeCodeAdapter`) reads real local fixture files from disk, and the
real `create_handoff_sandbox` orchestration wires all of that together
correctly end to end.

What this does NOT prove (explicitly, so this isn't mistaken for full
coverage): no real control-plane/sidecar/Kubernetes pod is involved, no
real `claude`/`codex`/`opencode` binary runs, and no real credential-broker
or audit-log behavior is exercised -- those were verified separately by
reading the actual control-plane source (see docs/handoff-adapters.md's
security-review section), not by running it here.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from boxkite_client import BoxkiteClient
from boxkite_handoff.adapters.claude_code import ClaudeCodeAdapter, encode_project_dir
from boxkite_handoff.orchestrator import create_handoff_sandbox

BASE_URL = "https://boxkite.example.test"
API_KEY = "test-api-key-123"


class FakeTakeoverWebsocket:
    """Stands in for websockets.sync.client.ClientConnection -- only the
    surface create_handoff_sandbox/orchestrator actually use."""

    def __init__(self, url: str, headers: dict[str, str]) -> None:
        self.url = url
        self.headers = headers
        self.sent: list[bytes] = []

    def send(self, data: bytes) -> None:
        self.sent.append(data)


class FakeHttpAndWs:
    """Records every real HTTP request BoxkiteClient makes (via
    httpx.MockTransport) and every websocket it opens (via a fake
    ws_connect), so the test can assert on the exact wire-level traffic a
    real handoff run produces."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.file_pushes: list[tuple[str, str]] = []  # (path, content)
        self.websockets: list[FakeTakeoverWebsocket] = []
        self._sandbox_counter = 0

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        method, path = request.method, request.url.path

        if method == "POST" and path == "/v1/sandboxes":
            self._sandbox_counter += 1
            sandbox_id = f"sandbox-{self._sandbox_counter}"
            return httpx.Response(200, json={"session_id": sandbox_id})

        if method == "POST" and path.endswith("/files"):
            body = json.loads(request.content)
            self.file_pushes.append((body["path"], body["content"]))
            return httpx.Response(
                200, json={"path": body["path"], "size": len(body["content"]), "created": True}
            )

        raise AssertionError(f"Unexpected request in fake server: {method} {path}")

    def connect_ws(self, url: str, **kwargs: Any) -> FakeTakeoverWebsocket:
        ws = FakeTakeoverWebsocket(url, kwargs.get("additional_headers", {}))
        self.websockets.append(ws)
        return ws


@pytest.fixture
def fake_server() -> FakeHttpAndWs:
    return FakeHttpAndWs()


@pytest.fixture
def real_client(fake_server: FakeHttpAndWs) -> BoxkiteClient:
    transport = httpx.MockTransport(fake_server.handle_request)
    return BoxkiteClient(
        base_url=BASE_URL,
        api_key=API_KEY,
        transport=transport,
        ws_connect=fake_server.connect_ws,
    )


@pytest.fixture
def claude_code_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-real-flow-token")
    config_dir = tmp_path / "claude-home"
    cwd = tmp_path / "myproject"
    cwd.mkdir()
    project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
    project_dir.mkdir(parents=True)
    session_file = project_dir / "abc-123.jsonl"
    session_file.write_text('{"type":"user","message":{"content":"hi"}}\n', encoding="utf-8")

    adapter = ClaudeCodeAdapter(config_dir=config_dir, cwd=cwd)
    return adapter.locate_session()


def test_real_client_creates_a_sandbox_via_a_real_http_request(
    real_client: BoxkiteClient, fake_server: FakeHttpAndWs, claude_code_session
) -> None:
    create_handoff_sandbox(real_client, claude_code_session)

    create_calls = [r for r in fake_server.requests if r.url.path == "/v1/sandboxes"]
    assert len(create_calls) == 1
    assert create_calls[0].headers["authorization"] == f"Bearer {API_KEY}"


def test_real_client_pushes_the_real_session_file_content(
    real_client: BoxkiteClient, fake_server: FakeHttpAndWs, claude_code_session
) -> None:
    create_handoff_sandbox(real_client, claude_code_session)

    session_file_pushes = [
        content for path, content in fake_server.file_pushes if path == claude_code_session.files[0].sandbox_path
    ]
    assert session_file_pushes == ['{"type":"user","message":{"content":"hi"}}\n']


def test_real_client_pushes_the_credential_as_a_separate_tmp_file_not_the_session_content(
    real_client: BoxkiteClient, fake_server: FakeHttpAndWs, claude_code_session
) -> None:
    create_handoff_sandbox(real_client, claude_code_session)

    credential_pushes = [
        (path, content) for path, content in fake_server.file_pushes if content == "sk-real-flow-token"
    ]
    assert len(credential_pushes) == 1
    credential_path, _ = credential_pushes[0]
    assert credential_path.startswith("/tmp/")


def test_real_client_opens_exactly_one_takeover_websocket_for_the_new_sandbox(
    real_client: BoxkiteClient, fake_server: FakeHttpAndWs, claude_code_session
) -> None:
    result = create_handoff_sandbox(real_client, claude_code_session)

    assert len(fake_server.websockets) == 1
    ws = fake_server.websockets[0]
    assert ws.url == f"wss://boxkite.example.test/v1/sandboxes/{result.sandbox_id}/takeover"
    assert ws.headers["Authorization"] == f"Bearer {API_KEY}"


def test_real_client_never_types_the_credential_value_and_types_the_real_resume_command(
    real_client: BoxkiteClient, fake_server: FakeHttpAndWs, claude_code_session
) -> None:
    create_handoff_sandbox(real_client, claude_code_session)

    ws = fake_server.websockets[0]
    for line in ws.sent:
        assert b"sk-real-flow-token" not in line
    assert ws.sent[-1] == f"claude --resume {claude_code_session.session_id}\n".encode("utf-8")
