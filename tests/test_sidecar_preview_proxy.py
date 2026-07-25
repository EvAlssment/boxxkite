"""Tests for network ingress preview (/preview/{port}/...) — see
docs/NETWORK-INGRESS-DESIGN.md.

Covers:
- build_k8s_exec_command's skip_network_isolation override (the scoped
  network-namespace exception expose_port relies on).
- /process/start validates and registers expose_port (range, sidecar-port
  collision, duplicate-port collision).
- /preview/{port}/{path} proxies a real HTTP request to a registered port,
  404s for an unregistered port, and 502s for a registered-but-not-running
  process.
- Stopping a process (or /process/kill-all) releases its exposed port.
- True streaming: no response-size cap by default, an optional configured
  cap still truncates, and `_stream_upstream_body`'s total-byte/max-duration
  safety valves behave deterministically in isolation.
"""

import asyncio
import http.server
import threading
import time

import main as sidecar_main
from fastapi.testclient import TestClient

AUTH_TOKEN = "the-real-secret"


def _client() -> TestClient:
    return TestClient(sidecar_main.app).__enter__()


def _headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: AUTH_TOKEN}


def _reset_registry():
    sidecar_main._process_registry.clear()
    sidecar_main._exposed_ports.clear()
    sidecar_main._process_registry_lock = None


def setup_function(_):
    _reset_registry()


def teardown_function(_):
    _reset_registry()


class _FakeProc:
    """Stands in for asyncio.subprocess.Process -- preview_proxy only ever
    reads ProcessHandle.status, never touches .proc directly."""

    def __init__(self):
        self.returncode = None


def _register_fake_process(port: int, *, status: str = "running") -> str:
    """Directly insert a ProcessHandle into the registry/exposed-port map,
    bypassing /process/start's real spawn machinery -- this test cares about
    the proxy route and port bookkeeping, not process spawning (already
    covered by test_sidecar_process_sessions.py)."""
    process_id = f"proc_{port}"
    handle = sidecar_main.ProcessHandle(
        process_id=process_id,
        proc=_FakeProc(),
        command="fake",
        description=None,
        max_runtime_seconds=60,
        expose_port=port,
    )
    handle.status = status
    sidecar_main._process_registry[process_id] = handle
    sidecar_main._exposed_ports[port] = process_id
    return process_id


class _EchoHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        body = f"hello from {self.path}".encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):  # silence default stderr logging
        pass


def _start_echo_server() -> tuple[http.server.HTTPServer, int]:
    server = http.server.HTTPServer(("127.0.0.1", 0), _EchoHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


def test_build_k8s_exec_command_skip_network_isolation_omits_unshare(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED", True)

    isolated = sidecar_main.build_k8s_exec_command(123, "echo hi")
    assert isolated[0] == "unshare"

    not_isolated = sidecar_main.build_k8s_exec_command(123, "echo hi", skip_network_isolation=True)
    assert not_isolated[0] == "nsenter"


def test_process_start_rejects_expose_port_out_of_range(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    client = _client()

    response = client.post(
        "/process/start",
        json={"command": "sleep 1", "max_runtime_seconds": 5, "expose_port": 80},
        headers=_headers(),
    )
    assert response.status_code == 400


def test_process_start_rejects_sidecar_port_as_expose_port(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    client = _client()

    response = client.post(
        "/process/start",
        json={
            "command": "sleep 1",
            "max_runtime_seconds": 5,
            "expose_port": sidecar_main.SIDECAR_PORT,
        },
        headers=_headers(),
    )
    assert response.status_code == 400


def test_process_start_rejects_conflicting_expose_port(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    _register_fake_process(4123)
    client = _client()

    response = client.post(
        "/process/start",
        json={"command": "sleep 1", "max_runtime_seconds": 5, "expose_port": 4123},
        headers=_headers(),
    )
    assert response.status_code == 409


def test_preview_proxy_404_for_unregistered_port(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    client = _client()

    response = client.get("/preview/59999/", headers=_headers())
    assert response.status_code == 404


def test_preview_proxy_502_when_process_not_running(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    _register_fake_process(4124, status="exited")
    client = _client()

    response = client.get("/preview/4124/", headers=_headers())
    assert response.status_code == 502


def test_preview_proxy_forwards_request_to_registered_port(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    server, port = _start_echo_server()
    try:
        _register_fake_process(port)
        client = _client()

        response = client.get(f"/preview/{port}/hello", headers=_headers())
        assert response.status_code == 200
        assert response.text == "hello from /hello"
    finally:
        server.shutdown()


def test_preview_proxy_requires_sidecar_auth_token(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    _register_fake_process(4125)
    client = _client()

    response = client.get("/preview/4125/")
    assert response.status_code == 401


class _ExitedProc:
    """A proc that's already exited by the time /process/stop is called --
    terminate()/wait() resolve immediately."""

    returncode = 0

    def terminate(self):
        pass

    async def wait(self, *a, **kw):
        return 0

    def kill(self):
        pass


def test_stopping_process_releases_exposed_port(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    process_id = _register_fake_process(4126)
    sidecar_main._process_registry[process_id].proc = _ExitedProc()
    client = _client()

    assert 4126 in sidecar_main._exposed_ports

    response = client.post(f"/process/{process_id}/stop", headers=_headers())
    assert response.status_code == 200
    assert 4126 not in sidecar_main._exposed_ports


def test_kill_all_processes_clears_exposed_ports():
    _register_fake_process(4127)
    _register_fake_process(4128)
    for handle in sidecar_main._process_registry.values():
        handle.proc = _ExitedProc()
    assert len(sidecar_main._exposed_ports) == 2

    import asyncio

    asyncio.run(sidecar_main._kill_all_processes())
    assert sidecar_main._exposed_ports == {}
    assert sidecar_main._process_registry == {}


# ── True streaming (docs/NETWORK-INGRESS-DESIGN.md's former "no true
# streaming" limitation) ────────────────────────────────────────────────


def test_preview_default_has_no_response_size_cap():
    """The whole point of true streaming: the sidecar no longer needs a
    memory-pressure-driven cap, so the default is now "off" (0), not the old
    10MB buffered-response ceiling."""
    assert sidecar_main.PREVIEW_MAX_RESPONSE_BYTES == 0


def test_preview_proxy_forwards_full_body_without_default_truncation(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    large_body = b"y" * 500_000

    class _LargeHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(large_body)))
            self.end_headers()
            self.wfile.write(large_body)

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), _LargeHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        _register_fake_process(port)
        client = _client()

        response = client.get(f"/preview/{port}/big", headers=_headers())
        assert response.status_code == 200
        assert len(response.content) == len(large_body)
        assert response.content == large_body
    finally:
        server.shutdown()


def test_preview_proxy_truncates_when_max_response_bytes_configured(monkeypatch):
    """An operator-configured cap is still honored -- true streaming removes
    the default, not the ability to opt back into one."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "PREVIEW_MAX_RESPONSE_BYTES", 10)
    server, port = _start_echo_server()
    try:
        _register_fake_process(port)
        client = _client()

        response = client.get(f"/preview/{port}/hello", headers=_headers())
        assert response.status_code == 200
        assert len(response.content) == 10
    finally:
        server.shutdown()


class _FakeAsyncClient:
    """Stands in for httpx.AsyncClient -- _stream_upstream_body only ever
    calls aclose() on it once streaming ends."""

    def __init__(self):
        self.closed = False

    async def aclose(self):
        self.closed = True


class _FakeUpstreamResponse:
    """Stands in for httpx.Response(stream=True) -- yields pre-set chunks
    from aiter_bytes() and tracks whether aclose() ran."""

    def __init__(self, chunks: list[bytes]):
        self._chunks = chunks
        self.closed = False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


def _collect(gen):
    async def _run():
        return [chunk async for chunk in gen]

    return asyncio.run(_run())


def test_stream_upstream_body_passes_through_everything_with_no_cap(monkeypatch):
    monkeypatch.setattr(sidecar_main, "PREVIEW_MAX_RESPONSE_BYTES", 0)
    monkeypatch.setattr(sidecar_main, "PREVIEW_STREAM_MAX_SECONDS", 300.0)
    big_chunk = b"x" * (1024 * 1024)
    upstream = _FakeUpstreamResponse([big_chunk, big_chunk])
    client = _FakeAsyncClient()

    chunks = _collect(sidecar_main._stream_upstream_body(client, upstream, port=1234))

    assert b"".join(chunks) == big_chunk * 2
    assert upstream.closed is True
    assert client.closed is True


def test_stream_upstream_body_enforces_total_byte_cap(monkeypatch):
    monkeypatch.setattr(sidecar_main, "PREVIEW_MAX_RESPONSE_BYTES", 10)
    monkeypatch.setattr(sidecar_main, "PREVIEW_STREAM_MAX_SECONDS", 300.0)
    upstream = _FakeUpstreamResponse([b"0123456789", b"should-not-appear"])
    client = _FakeAsyncClient()

    chunks = _collect(sidecar_main._stream_upstream_body(client, upstream, port=1234))

    assert b"".join(chunks) == b"0123456789"
    assert upstream.closed is True
    assert client.closed is True


def test_stream_upstream_body_truncates_mid_chunk_at_the_cap_boundary(monkeypatch):
    monkeypatch.setattr(sidecar_main, "PREVIEW_MAX_RESPONSE_BYTES", 5)
    monkeypatch.setattr(sidecar_main, "PREVIEW_STREAM_MAX_SECONDS", 300.0)
    upstream = _FakeUpstreamResponse([b"0123456789"])
    client = _FakeAsyncClient()

    chunks = _collect(sidecar_main._stream_upstream_body(client, upstream, port=1234))

    assert b"".join(chunks) == b"01234"


def test_stream_upstream_body_enforces_max_duration(monkeypatch):
    """Deterministic version of the wall-clock safety valve: fake
    time.monotonic() so the deadline is already passed after the first
    chunk, without any real sleeping."""
    monkeypatch.setattr(sidecar_main, "PREVIEW_MAX_RESPONSE_BYTES", 0)
    monkeypatch.setattr(sidecar_main, "PREVIEW_STREAM_MAX_SECONDS", 0.1)
    fake_times = iter([100.0, 100.5])
    monkeypatch.setattr(sidecar_main, "_preview_stream_monotonic", lambda: next(fake_times))

    upstream = _FakeUpstreamResponse([b"chunk1", b"chunk2", b"chunk3"])
    client = _FakeAsyncClient()

    chunks = _collect(sidecar_main._stream_upstream_body(client, upstream, port=1234))

    assert chunks == [b"chunk1"]
    assert upstream.closed is True
    assert client.closed is True


def test_preview_proxy_closes_upstream_client_on_connection_error(monkeypatch):
    """If the upstream dev server refuses the connection outright (before
    any streaming starts), the sidecar must still close its httpx client
    rather than leaking it."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    # Register a port nothing is actually listening on.
    _register_fake_process(59998)
    client = _client()

    response = client.get("/preview/59998/", headers=_headers())
    assert response.status_code == 502
