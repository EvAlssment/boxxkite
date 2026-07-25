"""Tests for the sidecar's GUI/remote-desktop human takeover endpoint
(WS /desktop) -- GitHub issue #184, docs/GUI-COMPUTER-USE-SCOPING.md.

Mirrors tests/test_sidecar_browser.py's/tests/test_sidecar_pty.py's coverage
shape:

- 404 (well, WS close code 4404) when BOXKITE_DESKTOP_ENABLED is off (the
  default).
- Auth is checked before accept()/before any stack is spawned.
- _ensure_desktop_stack_running spawns Xvfb -> WM -> x11vnc in that order,
  and is a no-op on a second call while the stack is still tracked as live.
- _ensure_desktop_stack_running raises OSError when a stage never comes up
  within its startup timeout.
- kill_desktop_session terminates every tracked process and clears the
  tracking dict; safe no-op when nothing is running.
- /configure kills any live desktop stack before wiping session state,
  unconditionally (regardless of the current BOXKITE_DESKTOP_ENABLED
  value) -- same cross-tenant-leak guard class as issue #130/#144's tmux
  fix.

No real Xvfb/x11vnc is spawned in these tests -- subprocess.Popen/
asyncio.create_subprocess_exec and asyncio.open_connection are mocked the
same way test_sidecar_browser.py mocks the CDP process.
"""

from __future__ import annotations

import asyncio

import main as sidecar_main
import pytest
import sidecar_desktop
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDisconnect

AUTH_TOKEN = "the-real-secret"


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: AUTH_TOKEN}


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


@pytest.fixture(autouse=True)
def _reset_desktop_state():
    sidecar_desktop._desktop_procs.clear()
    yield
    sidecar_desktop._desktop_procs.clear()


class _FakeProc:
    """Stands in for asyncio.subprocess.Process -- enough surface for
    _ensure_desktop_stack_running/kill_desktop_session."""

    def __init__(self) -> None:
        self.returncode = None
        self.killed = False

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode


def _patch_fast_startup(monkeypatch) -> None:
    """Make Xvfb-socket / VNC-port polling resolve immediately, so tests
    don't sleep for real."""
    monkeypatch.setattr(sidecar_desktop, "_wait_for_x11_socket", _noop_wait)
    monkeypatch.setattr(sidecar_desktop, "_wait_for_vnc_port", _noop_wait)


async def _noop_wait() -> None:
    return None


def test_desktop_ws_closes_4404_when_disabled(monkeypatch):
    monkeypatch.setattr(sidecar_main, "BOXKITE_DESKTOP_ENABLED", False)
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    client = _client()

    try:
        with client.websocket_connect("/desktop", headers=_auth_headers()):
            raise AssertionError("connection should have been rejected before accept")
    except WebSocketDisconnect as exc:
        assert exc.code == 4404


def test_desktop_ws_rejects_missing_auth_before_spawning_stack(monkeypatch):
    monkeypatch.setattr(sidecar_main, "BOXKITE_DESKTOP_ENABLED", True)
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)

    called = {"count": 0}

    async def _fake_ensure_stack():
        called["count"] += 1

    monkeypatch.setattr(sidecar_desktop, "_ensure_desktop_stack_running", _fake_ensure_stack)
    client = _client()

    try:
        with client.websocket_connect("/desktop"):
            raise AssertionError("connection should have been rejected before accept")
    except WebSocketDisconnect as exc:
        assert exc.code == 4401

    assert called["count"] == 0


def test_desktop_ws_fails_closed_when_token_unconfigured(monkeypatch):
    monkeypatch.setattr(sidecar_main, "BOXKITE_DESKTOP_ENABLED", True)
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "")
    client = _client()

    try:
        with client.websocket_connect("/desktop", headers=_auth_headers()):
            raise AssertionError("connection should have been rejected before accept")
    except WebSocketDisconnect as exc:
        assert exc.code == 1013


def test_ensure_desktop_stack_running_spawns_stages_in_order(monkeypatch):
    _patch_fast_startup(monkeypatch)
    spawned: list[tuple[str, list[str]]] = []

    async def _fake_spawn_stage(name, argv, *, extra_env=None):
        spawned.append((name, argv))
        sidecar_desktop._desktop_procs[name] = _FakeProc()

    monkeypatch.setattr(sidecar_desktop, "_spawn_desktop_stage", _fake_spawn_stage)

    asyncio.run(sidecar_desktop._ensure_desktop_stack_running())

    assert [name for name, _argv in spawned] == ["xvfb", "wm", "x11vnc"]
    assert spawned[0][1][0] == "Xvfb"
    assert spawned[1][1][0] == "fluxbox"
    assert spawned[2][1][0] == "x11vnc"


def test_ensure_desktop_stack_running_is_noop_when_already_live(monkeypatch):
    _patch_fast_startup(monkeypatch)
    spawn_calls = {"count": 0}

    async def _fake_spawn_stage(name, argv, *, extra_env=None):
        spawn_calls["count"] += 1
        sidecar_desktop._desktop_procs[name] = _FakeProc()

    monkeypatch.setattr(sidecar_desktop, "_spawn_desktop_stage", _fake_spawn_stage)

    asyncio.run(sidecar_desktop._ensure_desktop_stack_running())
    first_call_count = spawn_calls["count"]
    assert first_call_count == 3

    asyncio.run(sidecar_desktop._ensure_desktop_stack_running())
    assert spawn_calls["count"] == first_call_count, "second call must not respawn a live stack"


def test_ensure_desktop_stack_running_raises_oserror_when_vnc_never_comes_up(monkeypatch):
    monkeypatch.setattr(sidecar_desktop, "DESKTOP_STARTUP_TIMEOUT_SECONDS", 0)
    monkeypatch.setattr(sidecar_desktop, "_wait_for_x11_socket", _noop_wait)

    async def _fake_spawn_stage(name, argv, *, extra_env=None):
        sidecar_desktop._desktop_procs[name] = _FakeProc()

    monkeypatch.setattr(sidecar_desktop, "_spawn_desktop_stage", _fake_spawn_stage)

    async def _fake_open_connection(host, port):
        raise ConnectionRefusedError("nobody listening")

    monkeypatch.setattr(sidecar_desktop.asyncio, "open_connection", _fake_open_connection)

    with pytest.raises(OSError):
        asyncio.run(sidecar_desktop._ensure_desktop_stack_running())

    # Failure must not leave a half-up stack tracked as live.
    assert sidecar_desktop._desktop_procs == {}


def test_kill_desktop_session_terminates_all_tracked_processes():
    procs = {name: _FakeProc() for name in sidecar_desktop._DESKTOP_STAGE_ORDER}
    sidecar_desktop._desktop_procs.update(procs)

    asyncio.run(sidecar_desktop.kill_desktop_session())

    assert sidecar_desktop._desktop_procs == {}
    assert all(proc.killed for proc in procs.values())


def test_kill_desktop_session_tolerates_nothing_running():
    asyncio.run(sidecar_desktop.kill_desktop_session())  # must not raise
    assert sidecar_desktop._desktop_procs == {}


def test_kill_desktop_session_swallows_per_process_errors():
    class _BadProc(_FakeProc):
        def kill(self) -> None:
            raise RuntimeError("already reaped")

    sidecar_desktop._desktop_procs["xvfb"] = _BadProc()
    sidecar_desktop._desktop_procs["wm"] = _FakeProc()

    asyncio.run(sidecar_desktop.kill_desktop_session())  # must not raise

    assert sidecar_desktop._desktop_procs == {}


def test_configure_kills_desktop_session_before_wiping_session(monkeypatch, tmp_path):
    """Regression test for the cross-tenant leak this feature would
    otherwise introduce into pod recycling: /configure must kill the
    desktop stack so a recycled pod never hands a new tenant a still-live
    Xvfb/WM/x11vnc session left behind by a previous tenant."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 1)
    monkeypatch.setattr(
        sidecar_main, "build_k8s_exec_command", lambda pid, command: ["sh", "-c", command]
    )
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(sidecar_main, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(sidecar_main, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(sidecar_main, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(sidecar_main, "TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(sidecar_main.os, "chown", lambda *args, **kwargs: None)

    calls = []

    async def _fake_kill_desktop_session():
        calls.append(True)

    monkeypatch.setattr(sidecar_main, "kill_desktop_session", _fake_kill_desktop_session)

    # Plain (non-context-manager) TestClient -- see
    # test_configure_kills_takeover_tmux_session_before_wiping_session's own
    # comment on why entering/exiting the app lifespan is unrelated and a
    # pre-existing source of cross-test flakiness independent of this
    # feature.
    client = TestClient(sidecar_main.app)
    response = client.post(
        "/configure",
        json={
            "session_id": None,
            "organization_id": None,
            "work_item_id": None,
            "storage_prefix": None,
        },
        headers=_auth_headers(),
    )
    assert response.status_code == 200

    assert calls, "kill_desktop_session was not called by /configure"
