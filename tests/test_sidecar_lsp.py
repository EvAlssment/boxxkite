"""Tests for the sidecar's agent-invokable language server completions
(/lsp/*, sidecar_lsp.py) -- GitHub issue #183.

Mirrors tests/test_sidecar_node_interpreter.py's structure/conventions:

- 404 when BOXKITE_LSP_ENABLED is off (the default).
- Requires the same sidecar auth as every other route once enabled.
- The Content-Length JSON-RPC framing helpers round-trip correctly,
  including a real byte-length (not character-length) multi-byte UTF-8
  body.
- A notification frame (no `id`) is read and discarded without corrupting
  the next real response's correlation -- the core job of the reader loop.
- start -> open -> completion -> stop works end to end against a small
  fake language-server driver script that speaks real Content-Length
  JSON-RPC (same "bypass nsenter/docker-exec, run a local process instead"
  technique test_sidecar_node_interpreter.py/test_sidecar_interpreter.py
  already use -- there is no real sandbox container in this test
  environment).
- /configure kills any live LSP server before wiping session state -- the
  cross-tenant-leak regression test.
- Idle timeout kills the server (LSP_IDLE_TIMEOUT_SECONDS).
- The session exec budget wiring (/lsp/start, /lsp/{id}/completion) is
  covered in tests/test_sidecar_session_exec_budget.py, not duplicated
  here -- this file imports its fake driver helpers from there.

A real, non-mocked integration test against the actual `pyright-langserver`/
`typescript-language-server` binaries lives at the bottom of this file,
gated by `@pytest.mark.integration` and skipped when those binaries (or
network access to fetch them) aren't available.
"""

import asyncio
import json
import shutil
import subprocess
import sys
import textwrap

import main as sidecar_main
import pytest
import sidecar_lsp
from fastapi.testclient import TestClient

AUTH_TOKEN = "the-real-secret"


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: AUTH_TOKEN}


# ============================================================================
# Fake language-server driver: a small script speaking real Content-Length
# JSON-RPC over stdio, standing in for a real `pyright-langserver`/
# `typescript-language-server` process. Deliberately emits a spurious
# `window/logMessage` notification before responding to `initialize` (real
# servers do exactly this -- confirmed directly against a real
# pyright-langserver process) so every test that spawns through this driver
# already exercises the reader loop's notification-discard path, not just
# the dedicated regression test below.
# ============================================================================

FAKE_LSP_DRIVER_SOURCE = textwrap.dedent(
    """
    import json
    import sys


    def _read_frame():
        headers = {}
        while True:
            line = sys.stdin.buffer.readline()
            if not line:
                return None
            line = line.rstrip(b"\\r\\n")
            if line == b"":
                break
            key, _, value = line.partition(b":")
            headers[key.strip().lower()] = value.strip()
        length = int(headers.get(b"content-length", b"0"))
        body = sys.stdin.buffer.read(length)
        return json.loads(body.decode("utf-8"))


    def _write_frame(payload):
        body = json.dumps(payload).encode("utf-8")
        sys.stdout.buffer.write(("Content-Length: %d\\r\\n\\r\\n" % len(body)).encode("ascii") + body)
        sys.stdout.buffer.flush()


    def main():
        while True:
            msg = _read_frame()
            if msg is None:
                return
            method = msg.get("method")
            msg_id = msg.get("id")
            if method == "initialize":
                _write_frame({
                    "jsonrpc": "2.0",
                    "method": "window/logMessage",
                    "params": {"type": 3, "message": "fake server starting"},
                })
                _write_frame({"jsonrpc": "2.0", "id": msg_id, "result": {"capabilities": {}}})
            elif method in ("initialized", "textDocument/didOpen", "textDocument/didChange"):
                pass
            elif method == "textDocument/completion":
                _write_frame({
                    "jsonrpc": "2.0",
                    "method": "textDocument/publishDiagnostics",
                    "params": {"uri": "file:///workspace/probe.py", "diagnostics": []},
                })
                _write_frame({
                    "jsonrpc": "2.0",
                    "id": msg_id,
                    "result": {
                        "isIncomplete": False,
                        "items": [
                            {
                                "label": "fake_completion_item",
                                "kind": 3,
                                "detail": "() -> None",
                                "insertText": "fake_completion_item()",
                            },
                            {"label": "bare_label_item"},
                        ],
                    },
                })
            elif method == "shutdown":
                _write_frame({"jsonrpc": "2.0", "id": msg_id, "result": None})
            elif method == "exit":
                return
            elif msg_id is not None:
                _write_frame({"jsonrpc": "2.0", "id": msg_id, "result": None})


    if __name__ == "__main__":
        main()
    """
)


def _write_fake_driver(tmp_path) -> str:
    script_path = tmp_path / "fake_lsp_driver.py"
    script_path.write_text(FAKE_LSP_DRIVER_SOURCE)
    return str(script_path)


def _enable(monkeypatch, tmp_path):
    """Enable BOXKITE_LSP_ENABLED, bypass nsenter/docker-exec (no real
    sandbox container in this test environment, same technique every other
    sidecar test file uses), and point BOTH language slots at the fake
    driver script above so tests don't depend on real `pyright-langserver`/
    `typescript-language-server` binaries being installed."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "BOXKITE_LSP_ENABLED", True)
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 1)
    monkeypatch.setattr(
        sidecar_main, "build_k8s_exec_command", lambda pid, command: ["sh", "-c", command]
    )
    script_path = _write_fake_driver(tmp_path)
    fake_argv = [sys.executable, script_path]
    monkeypatch.setattr(
        sidecar_lsp,
        "_LSP_SERVER_COMMANDS",
        {"python": fake_argv, "typescript": fake_argv},
    )


# ============================================================================
# Framing helpers -- pure, no subprocess.
# ============================================================================


async def test_frame_message_and_read_one_frame_round_trip():
    payload = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"foo": "bar"}}
    framed = sidecar_lsp._frame_message(payload)

    reader = asyncio.StreamReader()
    reader.feed_data(framed)
    reader.feed_eof()

    result = await sidecar_lsp._read_one_frame(reader)
    assert result == payload


async def test_frame_message_uses_byte_length_not_character_length_for_multibyte_utf8():
    """A body containing multi-byte UTF-8 characters must be framed by its
    encoded BYTE length, not len(str) -- using the character count would
    desync the next frame's header from its body the moment any non-ASCII
    character (e.g. an emoji, or non-Latin source code) appears."""
    payload = {"jsonrpc": "2.0", "id": 2, "method": "textDocument/didOpen", "params": {"text": "héllo wörld 🎉"}}
    framed = sidecar_lsp._frame_message(payload)

    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header_line = framed.split(b"\r\n", 1)[0]
    declared_length = int(header_line.split(b":")[1].strip())
    assert declared_length == len(body)
    # The character count is smaller than the byte count -- é/ö are 2 bytes
    # each and 🎉 is 4 bytes, all counted as a single character by len().
    assert declared_length != len(json.dumps(payload, ensure_ascii=False))

    reader = asyncio.StreamReader()
    reader.feed_data(framed)
    reader.feed_eof()
    result = await sidecar_lsp._read_one_frame(reader)
    assert result == payload


async def test_read_one_frame_returns_none_on_clean_eof():
    reader = asyncio.StreamReader()
    reader.feed_eof()
    result = await sidecar_lsp._read_one_frame(reader)
    assert result is None


async def test_reader_loop_discards_notification_without_corrupting_next_response():
    """A frame with no `id` (a server notification) must be read and
    discarded, and the NEXT real response must still resolve its own
    pending future correctly -- the reader loop's core job."""
    reader = asyncio.StreamReader()

    class _FakeHandle:
        def __init__(self):
            self.lsp_id = "lsp_test"
            self.proc = type("P", (), {"stdout": reader})()
            self.pending = {}

    handle = _FakeHandle()
    loop = asyncio.get_event_loop()
    future = loop.create_future()
    handle.pending[7] = future

    reader.feed_data(
        sidecar_lsp._frame_message({"jsonrpc": "2.0", "method": "window/logMessage", "params": {}})
    )
    reader.feed_data(
        sidecar_lsp._frame_message({"jsonrpc": "2.0", "id": 7, "result": {"ok": True}})
    )
    reader.feed_eof()

    await sidecar_lsp._lsp_reader_loop(handle)

    assert future.done()
    assert future.result() == {"jsonrpc": "2.0", "id": 7, "result": {"ok": True}}


# ============================================================================
# Route-level gating
# ============================================================================


def test_lsp_start_404s_when_disabled_by_default(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    with TestClient(sidecar_main.app) as client:
        response = client.post("/lsp/start", json={"language": "python"}, headers=_auth_headers())
        assert response.status_code == 404


def test_lsp_open_404s_when_disabled_by_default(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/lsp/lsp_nonexistent/open", json={"path": "a.py", "content": "x"}, headers=_auth_headers()
        )
        assert response.status_code == 404


def test_lsp_completion_404s_when_disabled_by_default(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/lsp/lsp_nonexistent/completion",
            json={"path": "a.py", "line": 0, "character": 0},
            headers=_auth_headers(),
        )
        assert response.status_code == 404


def test_lsp_stop_404s_when_disabled_by_default(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    with TestClient(sidecar_main.app) as client:
        response = client.post("/lsp/lsp_nonexistent/stop", headers=_auth_headers())
        assert response.status_code == 404


def test_lsp_start_requires_auth_like_every_other_route(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    with TestClient(sidecar_main.app) as client:
        response = client.post("/lsp/start", json={"language": "python"})
        assert response.status_code == 401


def test_lsp_start_rejects_unsupported_language(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    with TestClient(sidecar_main.app) as client:
        response = client.post("/lsp/start", json={"language": "ruby"}, headers=_auth_headers())
        assert response.status_code == 400


# ============================================================================
# End-to-end against the fake driver
# ============================================================================


def test_lsp_full_lifecycle_start_open_completion_stop(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    with TestClient(sidecar_main.app) as client:
        start_response = client.post(
            "/lsp/start", json={"language": "python"}, headers=_auth_headers()
        )
        assert start_response.status_code == 201
        lsp_id = start_response.json()["lsp_id"]
        assert lsp_id.startswith("lsp_")

        open_response = client.post(
            f"/lsp/{lsp_id}/open",
            json={"path": "probe.py", "content": "import os\nos.pat"},
            headers=_auth_headers(),
        )
        assert open_response.status_code == 200
        assert open_response.json() == {"status": "opened"}

        completion_response = client.post(
            f"/lsp/{lsp_id}/completion",
            json={"path": "probe.py", "line": 1, "character": 6},
            headers=_auth_headers(),
        )
        assert completion_response.status_code == 200
        items = completion_response.json()["items"]
        labels = [item["label"] for item in items]
        assert "fake_completion_item" in labels
        assert "bare_label_item" in labels  # permissive item, missing optional fields

        # A second /open on the SAME path exercises didChange (not didOpen
        # again) -- not independently observable from outside the fake
        # driver, but must not error.
        second_open = client.post(
            f"/lsp/{lsp_id}/open",
            json={"path": "probe.py", "content": "import os\nos.path."},
            headers=_auth_headers(),
        )
        assert second_open.status_code == 200

        stop_response = client.post(f"/lsp/{lsp_id}/stop", headers=_auth_headers())
        assert stop_response.status_code == 200
        assert stop_response.json() == {"status": "stopped"}

        # The handle is gone -- a further call 404s.
        after_stop = client.post(
            f"/lsp/{lsp_id}/completion",
            json={"path": "probe.py", "line": 0, "character": 0},
            headers=_auth_headers(),
        )
        assert after_stop.status_code == 404


def test_lsp_completion_404s_for_unknown_lsp_id(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/lsp/lsp_does_not_exist/completion",
            json={"path": "a.py", "line": 0, "character": 0},
            headers=_auth_headers(),
        )
        assert response.status_code == 404


def test_lsp_stop_404s_for_unknown_lsp_id(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    with TestClient(sidecar_main.app) as client:
        response = client.post("/lsp/lsp_does_not_exist/stop", headers=_auth_headers())
        assert response.status_code == 404


def test_lsp_start_enforces_max_servers_cap(monkeypatch, tmp_path):
    _enable(monkeypatch, tmp_path)
    monkeypatch.setattr(sidecar_main, "LSP_MAX_SERVERS", 1)
    with TestClient(sidecar_main.app) as client:
        first = client.post("/lsp/start", json={"language": "python"}, headers=_auth_headers())
        assert first.status_code == 201

        second = client.post("/lsp/start", json={"language": "typescript"}, headers=_auth_headers())
        assert second.status_code == 429


def test_spawn_lsp_server_fails_when_sandbox_process_is_missing(monkeypatch):
    """K8s mode: if get_sandbox_pid() can't find the sandbox process,
    /lsp/start must fail loudly (502), not hang or silently no-op."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "BOXKITE_LSP_ENABLED", True)
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: None)

    with TestClient(sidecar_main.app) as client:
        response = client.post("/lsp/start", json={"language": "python"}, headers=_auth_headers())
        assert response.status_code == 502


# ============================================================================
# Idle reaping and cross-tenant teardown
# ============================================================================


async def test_lsp_idle_reaper_kills_process_past_timeout(monkeypatch, tmp_path):
    """Drives _spawn_lsp_server/_reap_idle_lsp_servers directly (no
    TestClient/HTTP) so this coroutine and the subprocess it awaits share
    one event loop -- same reasoning
    test_node_interpreter_idle_reaper_kills_process_past_timeout gives."""
    _enable(monkeypatch, tmp_path)
    monkeypatch.setattr(sidecar_main, "LSP_IDLE_TIMEOUT_SECONDS", 0)

    handle = await sidecar_lsp._spawn_lsp_server("python", "lsp_idle_test")
    async with sidecar_lsp._get_lsp_registry_lock():
        sidecar_main._lsp_registry["lsp_idle_test"] = handle
    assert handle.proc.returncode is None

    await sidecar_lsp._reap_idle_lsp_servers()

    assert "lsp_idle_test" not in sidecar_main._lsp_registry
    assert handle.proc.returncode is not None


def test_configure_kills_live_lsp_server_before_wiping_session(monkeypatch, tmp_path):
    """Regression test mirroring
    test_configure_kills_live_node_interpreter_before_wiping_session for LSP
    servers: a recycled pod must never hand a new tenant a still-live
    language server (with a previous tenant's file content already opened
    on it) left over from before."""
    _enable(monkeypatch, tmp_path)

    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(sidecar_main, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(sidecar_main, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(sidecar_main, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(sidecar_main, "TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(sidecar_main.os, "chown", lambda *args, **kwargs: None)

    with TestClient(sidecar_main.app) as client:
        start_response = client.post(
            "/lsp/start", json={"language": "python"}, headers=_auth_headers()
        )
        lsp_id = start_response.json()["lsp_id"]
        client.post(
            f"/lsp/{lsp_id}/open",
            json={"path": "tenant_a_secret.py", "content": "TENANT_A_SECRET = 1"},
            headers=_auth_headers(),
        )
        assert lsp_id in sidecar_main._lsp_registry

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

        assert sidecar_main._lsp_registry == {}

        # The old lsp_id is gone -- the next tenant gets a fresh one.
        after_configure = client.post(
            f"/lsp/{lsp_id}/completion",
            json={"path": "tenant_a_secret.py", "line": 0, "character": 0},
            headers=_auth_headers(),
        )
        assert after_configure.status_code == 404


# ============================================================================
# Real, non-mocked integration test (issue #183's actual ask: "real
# end-to-end LSP wired through the sidecar", not simulated). Uses the ACTUAL
# `pyright-langserver`/`typescript-language-server` binaries, fetched into a
# session-scoped local npm prefix on first use. Skips (rather than fails)
# when network/npm access isn't available in this environment -- same
# graceful-skip posture as any test depending on an external toolchain not
# guaranteed present everywhere this suite runs.
#
# Like every other test in this file (and every other sidecar test in this
# repo), this bypasses nsenter/docker-exec -- there is no real Kubernetes
# pod or Docker sandbox container in this test environment, and no test
# anywhere in this suite spins one up. What makes this test genuinely
# "real, non-mocked" is that the language server process itself is the
# actual `pyright-langserver`/`typescript-language-server` binary doing
# actual source analysis, not the fake driver script above.
# ============================================================================


def _npm_install_lsp_binaries(prefix_dir) -> bool:
    """Best-effort local npm install of pyright + typescript-language-server
    (pinned to typescript@5 -- typescript@7's rewritten CLI does not ship
    tsserver.js, which typescript-language-server requires; confirmed
    directly). Returns True on success, False if npm/network isn't
    available (never raises -- callers use this to decide whether to skip).
    """
    if shutil.which("npm") is None:
        return False
    try:
        subprocess.run(
            [
                "npm", "install", "--no-save", "--no-audit", "--no-fund",
                "--prefix", str(prefix_dir),
                "pyright", "typescript-language-server", "typescript@5",
            ],
            cwd=str(prefix_dir),
            check=True,
            capture_output=True,
            timeout=120,
        )
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return False
    return True


@pytest.fixture(scope="module")
def real_lsp_binaries(tmp_path_factory):
    """Returns {"python": <path to real pyright-langserver>, "typescript":
    <path to real typescript-language-server>} -- session-scoped so the
    (network-dependent) npm install only runs once for both integration
    tests below."""
    prefix_dir = tmp_path_factory.mktemp("lsp-real-binaries")
    if not _npm_install_lsp_binaries(prefix_dir):
        pytest.skip("npm/network unavailable; cannot fetch real LSP server binaries")
    bin_dir = prefix_dir / "node_modules" / ".bin"
    pyright = bin_dir / "pyright-langserver"
    ts_server = bin_dir / "typescript-language-server"
    if not (pyright.exists() and ts_server.exists()):
        pytest.skip("npm install did not produce the expected LSP server binaries")
    return {"python": str(pyright), "typescript": str(ts_server)}


def _extend_safe_exec_env_path_for_node(monkeypatch):
    """SAFE_EXEC_ENV's PATH is deliberately restricted to where a real
    sandbox container installs `node` -- these tests bypass nsenter/
    docker-exec entirely (no sandbox container here) and run a plain local
    shell instead, so the real language server's `#!/usr/bin/env node`
    shebang needs `node` reachable via whatever PATH this dev/CI machine
    actually has it on. Same technique
    test_sidecar_node_interpreter.py's `_enable` uses."""
    node_path = shutil.which("node")
    if node_path:
        node_dir = node_path.rsplit("/", 1)[0]
        extended_env = dict(sidecar_main.SAFE_EXEC_ENV)
        extended_env["PATH"] = f"{extended_env['PATH']}:{node_dir}"
        monkeypatch.setattr(sidecar_main, "SAFE_EXEC_ENV", extended_env)


@pytest.mark.integration
def test_real_pyright_langserver_returns_a_real_completion_item(monkeypatch, tmp_path, real_lsp_binaries):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "BOXKITE_LSP_ENABLED", True)
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 1)
    monkeypatch.setattr(
        sidecar_main, "build_k8s_exec_command", lambda pid, command: ["sh", "-c", command]
    )
    _extend_safe_exec_env_path_for_node(monkeypatch)
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(
        sidecar_lsp,
        "_LSP_SERVER_COMMANDS",
        {
            "python": [real_lsp_binaries["python"], "--stdio"],
            "typescript": [real_lsp_binaries["typescript"], "--stdio"],
        },
    )
    monkeypatch.setattr(sidecar_main, "LSP_STARTUP_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(sidecar_main, "LSP_REQUEST_TIMEOUT_SECONDS", 30)

    with TestClient(sidecar_main.app) as client:
        start_response = client.post(
            "/lsp/start", json={"language": "python"}, headers=_auth_headers()
        )
        assert start_response.status_code == 201
        lsp_id = start_response.json()["lsp_id"]

        client.post(
            f"/lsp/{lsp_id}/open",
            json={"path": "real_probe.py", "content": "import os\nos.pat"},
            headers=_auth_headers(),
        )

        completion_response = None
        for _ in range(10):
            completion_response = client.post(
                f"/lsp/{lsp_id}/completion",
                json={"path": "real_probe.py", "line": 1, "character": 6},
                headers=_auth_headers(),
            )
            items = completion_response.json().get("items", [])
            if any("path" in (item.get("label") or "") for item in items):
                break
        else:
            pytest.fail(f"real pyright-langserver never returned a 'path' completion: {items}")

        assert completion_response.status_code == 200

        client.post(f"/lsp/{lsp_id}/stop", headers=_auth_headers())


@pytest.mark.integration
def test_real_typescript_language_server_returns_a_real_completion_item(
    monkeypatch, tmp_path, real_lsp_binaries
):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "BOXKITE_LSP_ENABLED", True)
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 1)
    monkeypatch.setattr(
        sidecar_main, "build_k8s_exec_command", lambda pid, command: ["sh", "-c", command]
    )
    _extend_safe_exec_env_path_for_node(monkeypatch)
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path))
    monkeypatch.setattr(
        sidecar_lsp,
        "_LSP_SERVER_COMMANDS",
        {
            "python": [real_lsp_binaries["python"], "--stdio"],
            "typescript": [real_lsp_binaries["typescript"], "--stdio"],
        },
    )
    monkeypatch.setattr(sidecar_main, "LSP_STARTUP_TIMEOUT_SECONDS", 60)
    monkeypatch.setattr(sidecar_main, "LSP_REQUEST_TIMEOUT_SECONDS", 30)

    with TestClient(sidecar_main.app) as client:
        start_response = client.post(
            "/lsp/start", json={"language": "typescript"}, headers=_auth_headers()
        )
        assert start_response.status_code == 201
        lsp_id = start_response.json()["lsp_id"]

        client.post(
            f"/lsp/{lsp_id}/open",
            json={"path": "real_probe.ts", "content": "const x = { path: 1, parse: 2 };\nx.pa"},
            headers=_auth_headers(),
        )

        completion_response = None
        for _ in range(10):
            completion_response = client.post(
                f"/lsp/{lsp_id}/completion",
                json={"path": "real_probe.ts", "line": 1, "character": 4},
                headers=_auth_headers(),
            )
            items = completion_response.json().get("items", [])
            if any((item.get("label") or "") == "path" for item in items):
                break
        else:
            pytest.fail(f"real typescript-language-server never returned a 'path' completion: {items}")

        assert completion_response.status_code == 200

        client.post(f"/lsp/{lsp_id}/stop", headers=_auth_headers())
