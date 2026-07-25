"""Tests for SandboxManager's background process/session client methods
(start_process/get_process_output/send_process_input/stop_process/
list_processes) and the mandatory kill-on-teardown wiring into
destroy_session/_recycle_pod_via_k8s. See docs/PROCESS-SESSIONS-DESIGN.md.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from boxkite.manager import SandboxManager


def _compose_manager_with_session(session_id: str = "session-1") -> SandboxManager:
    manager = SandboxManager()
    manager._use_docker_compose = True
    manager._compose_sessions[session_id] = {"organization_id": "org-1"}
    manager._cache_session_endpoint(session_id, "compose-sandbox", "localhost")
    return manager


class _FakeResponse:
    def __init__(self, json_body, status_code=200):
        self._json_body = json_body
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._json_body


@pytest.mark.asyncio
async def test_start_process_posts_expected_payload(monkeypatch):
    manager = _compose_manager_with_session()
    fake_client = SimpleNamespace(
        post=AsyncMock(
            return_value=_FakeResponse(
                {"process_id": "proc_abc", "status": "running", "started_at": "2026-07-11T00:00:00"}
            )
        )
    )
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_client)

    result = await manager.start_process(
        session_id="session-1",
        command="npm run dev",
        description="dev server",
        max_runtime_seconds=3600,
    )

    assert result["process_id"] == "proc_abc"
    fake_client.post.assert_awaited_once_with(
        "/process/start",
        json={
            "command": "npm run dev",
            "description": "dev server",
            "max_runtime_seconds": 3600,
            "expose_port": None,
        },
    )


@pytest.mark.asyncio
async def test_start_process_forwards_expose_port(monkeypatch):
    """See docs/NETWORK-INGRESS-DESIGN.md -- expose_port must reach the
    sidecar's /process/start payload unchanged."""
    manager = _compose_manager_with_session()
    fake_client = SimpleNamespace(
        post=AsyncMock(
            return_value=_FakeResponse(
                {"process_id": "proc_abc", "status": "running", "started_at": "2026-07-11T00:00:00"}
            )
        )
    )
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_client)

    await manager.start_process(
        session_id="session-1",
        command="npm run dev",
        max_runtime_seconds=3600,
        expose_port=3000,
    )

    fake_client.post.assert_awaited_once_with(
        "/process/start",
        json={
            "command": "npm run dev",
            "description": None,
            "max_runtime_seconds": 3600,
            "expose_port": 3000,
        },
    )


@pytest.mark.asyncio
async def test_proxy_preview_request_forwards_to_sidecar_preview_path(monkeypatch):
    """True streaming (docs/NETWORK-INGRESS-DESIGN.md's former "no true
    streaming" limitation, closed by this change): the manager now builds a
    request and sends it with stream=True instead of the old buffered
    client.request() call, so the caller can drain the body incrementally."""
    manager = _compose_manager_with_session()
    fake_request = object()
    fake_response = object()
    # build_request is synchronous on a real httpx.AsyncClient -- match that.
    fake_client = SimpleNamespace(
        build_request=lambda *a, **k: fake_request,
        send=AsyncMock(return_value=fake_response),
    )
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_client)

    result = await manager.proxy_preview_request(
        session_id="session-1",
        port=3000,
        path="/assets/app.js",
        method="GET",
        params={"v": "1"},
        headers={"accept": "text/javascript"},
        content=b"",
    )

    assert result is fake_response
    fake_client.send.assert_awaited_once_with(fake_request, stream=True)


@pytest.mark.asyncio
async def test_proxy_preview_request_builds_request_with_normalized_path(monkeypatch):
    manager = _compose_manager_with_session()
    captured_build_request_calls = []

    def _fake_build_request(method, url, **kwargs):
        captured_build_request_calls.append((method, url, kwargs))
        return object()

    fake_client = SimpleNamespace(
        build_request=_fake_build_request, send=AsyncMock(return_value=object())
    )
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_client)

    await manager.proxy_preview_request(
        session_id="session-1", port=3000, path="", method="GET"
    )

    assert len(captured_build_request_calls) == 1
    method, url, kwargs = captured_build_request_calls[0]
    assert method == "GET"
    assert url == "/preview/3000/"
    assert kwargs == {"params": None, "headers": None, "content": b""}


@pytest.mark.asyncio
async def test_proxy_preview_request_strips_leading_slash_from_path(monkeypatch):
    manager = _compose_manager_with_session()
    captured_build_request_calls = []

    def _fake_build_request(method, url, **kwargs):
        captured_build_request_calls.append(url)
        return object()

    fake_client = SimpleNamespace(
        build_request=_fake_build_request, send=AsyncMock(return_value=object())
    )
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_client)

    await manager.proxy_preview_request(
        session_id="session-1", port=3000, path="/assets/app.js", method="GET"
    )

    assert captured_build_request_calls == ["/preview/3000/assets/app.js"]


@pytest.mark.asyncio
async def test_get_process_output_passes_since_offset(monkeypatch):
    manager = _compose_manager_with_session()
    fake_client = SimpleNamespace(
        get=AsyncMock(
            return_value=_FakeResponse(
                {
                    "status": "running",
                    "stdout_chunk": "hello\n",
                    "next_offset": 6,
                    "truncated": False,
                    "exit_code": None,
                }
            )
        )
    )
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_client)

    result = await manager.get_process_output(
        session_id="session-1", process_id="proc_abc", since_offset=3
    )

    assert result["stdout_chunk"] == "hello\n"
    fake_client.get.assert_awaited_once_with(
        "/process/proc_abc/output", params={"since_offset": 3}
    )


@pytest.mark.asyncio
async def test_get_process_output_raises_value_error_on_404(monkeypatch):
    manager = _compose_manager_with_session()
    fake_client = SimpleNamespace(
        get=AsyncMock(return_value=_FakeResponse({"detail": "Process not found"}, status_code=404))
    )
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_client)

    with pytest.raises(ValueError, match="not found"):
        await manager.get_process_output(session_id="session-1", process_id="proc_missing")


@pytest.mark.asyncio
async def test_send_process_input_posts_data(monkeypatch):
    manager = _compose_manager_with_session()
    fake_client = SimpleNamespace(
        post=AsyncMock(return_value=_FakeResponse({"bytes_written": 2}))
    )
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_client)

    result = await manager.send_process_input(
        session_id="session-1", process_id="proc_abc", data="y\n"
    )

    assert result["bytes_written"] == 2
    fake_client.post.assert_awaited_once_with("/process/proc_abc/input", json={"data": "y\n"})


@pytest.mark.asyncio
async def test_stop_process_posts_to_stop_route(monkeypatch):
    manager = _compose_manager_with_session()
    fake_client = SimpleNamespace(
        post=AsyncMock(return_value=_FakeResponse({"status": "stopped", "exit_code": 143}))
    )
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_client)

    result = await manager.stop_process(session_id="session-1", process_id="proc_abc")

    assert result["status"] == "stopped"
    fake_client.post.assert_awaited_once_with("/process/proc_abc/stop")


@pytest.mark.asyncio
async def test_list_processes_returns_processes_list(monkeypatch):
    manager = _compose_manager_with_session()
    fake_client = SimpleNamespace(
        get=AsyncMock(
            return_value=_FakeResponse(
                {"processes": [{"process_id": "proc_abc", "status": "running"}]}
            )
        )
    )
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_client)

    result = await manager.list_processes(session_id="session-1")

    assert result == [{"process_id": "proc_abc", "status": "running"}]
    fake_client.get.assert_awaited_once_with("/process")


@pytest.mark.asyncio
async def test_kill_all_processes_posts_to_kill_all_route(monkeypatch):
    manager = SandboxManager()
    fake_client = SimpleNamespace(post=AsyncMock(return_value=_FakeResponse({"killed": 2})))
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_client)

    await manager._kill_all_processes("sandbox-pod", "10.8.0.80")

    fake_client.post.assert_awaited_once_with("/process/kill-all")


@pytest.mark.asyncio
async def test_kill_all_processes_is_best_effort_on_failure(monkeypatch):
    """A failed kill-all call must not raise -- teardown must still proceed
    (/configure's own internal kill-all is defense in depth for exactly this
    case; see _kill_all_processes()'s docstring)."""
    manager = SandboxManager()

    async def _raise(*_a, **_k):
        raise RuntimeError("sidecar unreachable")

    fake_client = SimpleNamespace(post=_raise)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_client)

    await manager._kill_all_processes("sandbox-pod", "10.8.0.80")  # must not raise


@pytest.mark.asyncio
async def test_destroy_session_kills_processes_before_flush(monkeypatch):
    """Regression test for the ordering docs/PROCESS-SESSIONS-DESIGN.md
    requires: background processes must be killed before flush/teardown, not
    after, so a process mid-write to disk doesn't race the flush."""
    manager = _compose_manager_with_session()

    call_order = []

    async def _post(path, *_a, **_k):
        call_order.append(path)
        return _FakeResponse({"killed": 1, "status": "flushed"})

    fake_client = SimpleNamespace(post=_post)
    monkeypatch.setattr(manager, "_get_http_client", lambda *_a, **_k: fake_client)

    await manager.destroy_session("session-1")

    assert call_order == ["/process/kill-all", "/flush"]


@pytest.mark.asyncio
async def test_recycle_pod_via_k8s_kills_processes_before_configure(monkeypatch):
    """Regression test for the highest-severity risk in
    docs/PROCESS-SESSIONS-DESIGN.md section 5: a recycled pod must never
    still have a live (or readable) background process from the tenant it's
    being taken away from."""
    manager = SandboxManager()
    manager.WARM_POOL_RECYCLE = True
    monkeypatch.setattr("boxkite.manager.WARM_POOL_RECYCLE", True)
    monkeypatch.setattr("boxkite.manager.WARM_POOL_MAX", 100)

    call_order = []

    async def _fake_kill_all_processes(pod_name, pod_ip):
        call_order.append("kill-all")

    async def _fake_init_k8s():
        manager._k8s_core_api = SimpleNamespace(
            list_namespaced_pod=AsyncMock(
                return_value=SimpleNamespace(items=[SimpleNamespace(status=SimpleNamespace(phase="Running"))])
            ),
            patch_namespaced_pod=AsyncMock(),
        )

    class _FakeConfigureResponse:
        def raise_for_status(self):
            return None

    class _FakeHttpxClient:
        def __init__(self, *_a, **_k):
            call_order.append("configure-client-created")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def post(self, *_a, **_k):
            call_order.append("configure")
            return _FakeConfigureResponse()

    async def _fake_ensure_pod_tls_cert_cached(pod_name):
        return ""

    monkeypatch.setattr(manager, "_kill_all_processes", _fake_kill_all_processes)
    monkeypatch.setattr(manager, "_init_k8s", _fake_init_k8s)
    monkeypatch.setattr(manager, "_auth_headers_for_pod", lambda *_a, **_k: {})
    monkeypatch.setattr(manager, "_ensure_pod_tls_cert_cached", _fake_ensure_pod_tls_cert_cached)
    monkeypatch.setattr("boxkite.manager.httpx.AsyncClient", _FakeHttpxClient)

    recycled = await manager._recycle_pod_via_k8s("sandbox-pod", "10.8.0.80")

    assert recycled is True
    assert call_order.index("kill-all") < call_order.index("configure")
