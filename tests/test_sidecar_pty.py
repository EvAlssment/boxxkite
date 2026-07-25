"""Tests for the sidecar's interactive PTY takeover endpoint (WS /pty).

Covers:
- An unauthenticated (or wrongly authenticated) WebSocket connection is
  rejected BEFORE accept()/handshake completion, not after a PTY has been
  allocated (see docs/SANDBOX-OBSERVABILITY-DESIGN.md §4).
- The sidecar fails CLOSED (matching /exec's 503 behavior) when no
  SIDECAR_AUTH_TOKEN is configured at all.
- A correctly authenticated connection gets a real, working interactive
  shell: bytes sent over the socket are written to the PTY's stdin and the
  PTY's output is relayed back as WS binary frames.
- GitHub issue #130: the default takeover command is `tmux new-session -A
  -s takeover ...` (attach-or-create), not a bare shell, and a dropped
  WebSocket reattaches to the SAME live tmux session -- shell state (an
  exported env var) set before a disconnect is still visible after
  reconnecting -- instead of losing it. Also covers the security follow-up
  that persistence requires: the takeover tmux session must be killed on
  /configure (pod recycle), or it would leak one tenant's live shell to
  the next tenant claiming this pod.
- GitHub issue #144 (CRITICAL fix on top of #130): tmux must run as the
  SIDECAR's own process, on an explicit socket path outside every volume
  shared with the sandbox container -- never nsentered/docker-exec'd INTO
  the sandbox namespace as the sandbox UID, which is what the first pass
  of #130 did and which let any same-UID sandboxed process
  (bash_tool/exec) reach the operator's live takeover session with zero
  privilege. Covers: the constructed command always has `tmux` as argv[0]
  wrapping the nsenter/docker-exec entry (never the other way around);
  kill_takeover_tmux_session now runs tmux directly, not via
  exec_in_sandbox; and an empirical isolation reproduction proving a
  process using tmux's default socket resolution (what a same-UID
  sandboxed process would be limited to, having no way to learn a path
  outside every volume it can see) cannot see or attach to a session
  living on the explicit socket path.

These tests bypass build_pty_command's nsenter/docker-exec namespace-entry
step (there is no sandbox container present in this test environment) by
monkeypatching it to spawn a real local /bin/bash (or, for the reattach
tests, a real local tmux) directly -- everything downstream of that (PTY
allocation, the asyncio bridging, auth-before-accept) is exercised for real,
against a real shell/tmux process.
"""

import asyncio
import shutil
import subprocess
import time
import uuid

import pytest

import main as sidecar_main
import sidecar_pty
from fastapi.testclient import TestClient
from starlette.testclient import WebSocketDisconnect

_TMUX_BIN = shutil.which("tmux")
requires_tmux = pytest.mark.skipif(
    _TMUX_BIN is None, reason="tmux is not installed on this test runner"
)


def _short_socket_path(name: str) -> str:
    """A tmux/AF_UNIX control-socket path short enough for `sun_path`'s
    ~104-byte limit (macOS/BSD; Linux allows more, but there's no reason to
    depend on that difference). pytest's own `tmp_path` fixture nests deep
    enough (.../pytest-of-<user>/pytest-<n>/<test-name>.../) to blow this
    limit on macOS -- `bind: File name too long` -- long before any of this
    feature's own code is exercised, so these socket-bearing tests use
    `/tmp` directly instead."""
    return f"/tmp/bk-pty-{uuid.uuid4().hex[:10]}-{name}.sock"


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def test_pty_rejects_connection_with_no_auth_header(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    client = _client()

    try:
        with client.websocket_connect("/pty"):
            raise AssertionError("connection should have been rejected before accept")
    except WebSocketDisconnect as exc:
        assert exc.code == 4401


def test_pty_rejects_connection_with_wrong_token(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    client = _client()

    try:
        with client.websocket_connect(
            "/pty", headers={sidecar_main.SIDECAR_AUTH_HEADER: "totally-wrong-value"}
        ):
            raise AssertionError("connection should have been rejected before accept")
    except WebSocketDisconnect as exc:
        assert exc.code == 4401


def test_pty_fails_closed_when_token_unconfigured(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "")
    client = _client()

    try:
        with client.websocket_connect("/pty", headers=_auth_headers()):
            raise AssertionError("connection should have been rejected before accept")
    except WebSocketDisconnect as exc:
        assert exc.code == 1013


def test_pty_auth_is_checked_before_command_build(monkeypatch):
    """The auth check must happen before build_pty_command / PTY allocation
    -- assert build_pty_command is never even called on a rejected connection."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")

    called = {"count": 0}

    def _fake_build_pty_command():
        called["count"] += 1
        return ["/bin/bash", "--norc", "--noprofile"]

    monkeypatch.setattr(sidecar_main, "build_pty_command", _fake_build_pty_command)
    client = _client()

    try:
        with client.websocket_connect("/pty"):
            pass
    except WebSocketDisconnect:
        pass

    assert called["count"] == 0


def test_pty_bridges_real_shell_command_output(monkeypatch):
    """End-to-end: bytes written to the socket reach the shell's stdin, and
    the shell's stdout comes back over the socket as binary frames."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setattr(
        sidecar_main,
        "build_pty_command",
        lambda: ["/bin/bash", "--norc", "--noprofile"],
    )
    client = _client()

    marker = "hello_from_pty_test_12345"
    with client.websocket_connect("/pty", headers=_auth_headers()) as ws:
        ws.send_bytes(f"echo {marker}\n".encode())

        collected = b""
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                chunk = ws.receive_bytes()
            except WebSocketDisconnect:
                break
            collected += chunk
            if marker.encode() in collected:
                break

        assert marker.encode() in collected


def test_pty_closes_shell_on_disconnect(monkeypatch):
    """When the client disconnects, the underlying shell process must be
    terminated rather than left running (leak check)."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")

    spawned_procs = []
    real_popen = sidecar_main.subprocess.Popen

    def _tracking_popen(*args, **kwargs):
        proc = real_popen(*args, **kwargs)
        spawned_procs.append(proc)
        return proc

    monkeypatch.setattr(sidecar_main.subprocess, "Popen", _tracking_popen)
    monkeypatch.setattr(
        sidecar_main,
        "build_pty_command",
        lambda: ["/bin/bash", "--norc", "--noprofile"],
    )
    client = _client()

    with client.websocket_connect("/pty", headers=_auth_headers()) as ws:
        ws.send_bytes(b"echo ready\n")
        # Give the shell a moment to start before we tear the connection down.
        time.sleep(0.2)

    assert spawned_procs, "expected the PTY endpoint to spawn a shell process"
    deadline = time.monotonic() + 5
    proc = spawned_procs[0]
    while time.monotonic() < deadline and proc.poll() is None:
        time.sleep(0.1)
    assert proc.poll() is not None, "shell process was not reaped after client disconnect"


# ============================================================================
# GitHub issue #130: tmux-backed session persistence across WS reconnects
# ============================================================================

def test_build_pty_command_wraps_nsenter_in_tmux_in_compose_mode_too(monkeypatch, tmp_path):
    """GitHub issue #144 fix: tmux must be argv[0], running as the sidecar's
    own process on the explicit socket path, with the nsenter entry into the
    sandbox as tmux's OWN pane command (after `--`) -- the exact inverse of
    the first (broken) pass, which nsentered tmux itself into the sandbox.

    Compose mode used `docker exec` here until deploy/docker-compose.yml
    started sharing a PID namespace with the sandbox container (`pid:
    "container:sandbox"`) -- see get_sandbox_pid's docstring. `docker exec`
    always joined the target container's *existing* network namespace with
    no way to give it a fresh one per call, which was a real, disclosed
    compose-only isolation gap (SECURITY.md). Both runtime modes now go
    through the identical nsenter path, asserted here explicitly rather than
    just relying on the k8s-mode test below to cover it."""
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "compose")
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 4242)
    monkeypatch.setattr(sidecar_main, "SANDBOX_UID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_GID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED", False)
    monkeypatch.setattr(sidecar_pty, "_ensure_takeover_tmux_socket_dir", lambda: None)

    cmd = sidecar_pty.build_pty_command()

    assert cmd == [
        "tmux", "-f", "/dev/null", "-S", sidecar_pty.TAKEOVER_TMUX_SOCKET,
        "new-session", "-A", "-s", "takeover",
        "--", "nsenter", "-t", "4242", "-m", "-p",
        "--setuid", "1001", "--setgid", "1001",
        "--", "/bin/bash",
    ]
    # tmux is the outermost command -- never wrapped by nsenter.
    assert cmd[0] == "tmux"
    assert cmd.index("nsenter") > cmd.index("tmux")


def test_build_pty_command_wraps_nsenter_in_tmux_in_k8s_mode(monkeypatch):
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "k8s")
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 4242)
    monkeypatch.setattr(sidecar_main, "SANDBOX_UID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_GID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED", False)
    monkeypatch.setattr(sidecar_pty, "_ensure_takeover_tmux_socket_dir", lambda: None)

    cmd = sidecar_pty.build_pty_command()

    assert cmd == [
        "tmux", "-f", "/dev/null", "-S", sidecar_pty.TAKEOVER_TMUX_SOCKET,
        "new-session", "-A", "-s", "takeover",
        "--", "nsenter", "-t", "4242", "-m", "-p",
        "--setuid", "1001", "--setgid", "1001",
        "--", "/bin/bash",
    ]
    # tmux is the outermost command -- never wrapped by nsenter, i.e. tmux
    # itself is never run inside the sandbox's own namespace/UID.
    assert cmd[0] == "tmux"
    assert cmd.index("nsenter") > cmd.index("tmux")


def test_build_pty_command_wraps_unshare_nsenter_in_tmux_when_network_isolation_enabled(monkeypatch):
    """The existing SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED unshare wrapping
    must still apply to the nsenter/shell part exactly as before -- tmux
    itself needs no network isolation, it's the shell entered inside it
    that does."""
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "k8s")
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 4242)
    monkeypatch.setattr(sidecar_main, "SANDBOX_UID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_GID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED", True)
    monkeypatch.setattr(sidecar_pty, "_ensure_takeover_tmux_socket_dir", lambda: None)

    cmd = sidecar_pty.build_pty_command()

    assert cmd == [
        "tmux", "-f", "/dev/null", "-S", sidecar_pty.TAKEOVER_TMUX_SOCKET,
        "new-session", "-A", "-s", "takeover",
        "--", "unshare", "-n", "nsenter", "-t", "4242", "-m", "-p",
        "--setuid", "1001", "--setgid", "1001",
        "--", "/bin/bash",
    ]


def test_build_pty_command_exec_argv_path_is_unaffected_by_tmux_wrapping(monkeypatch):
    """/pty-exec passes its own argv explicitly -- it must never be
    wrapped in tmux at all, and must go straight through the plain
    nsenter entry, same as before issue #130. Compose mode now goes
    through the identical nsenter path as k8s mode (see get_sandbox_pid's
    docstring), so this asserts the nsenter shape rather than the old,
    now-removed docker-exec fallback."""
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "compose")
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 4242)
    monkeypatch.setattr(sidecar_main, "SANDBOX_UID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_GID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED", False)

    cmd = sidecar_pty.build_pty_command(["echo", "hi"])

    assert cmd == [
        "nsenter", "-t", "4242", "-m", "-p",
        "--setuid", "1001", "--setgid", "1001",
        "--", "echo", "hi",
    ]
    assert "tmux" not in cmd


def test_takeover_tmux_socket_path_is_outside_every_sandbox_shared_volume():
    """Static guard against regressing GitHub issue #144: the takeover
    socket must not live under /tmp, /workspace, /mnt/user-data, /mnt/skills,
    or /var/run/docker.sock's directory -- every path deploy/pod-template.yaml
    and/or deploy/docker-compose.yml mount into BOTH the sandbox and sidecar
    containers."""
    shared_prefixes = ("/tmp", "/workspace", "/mnt", "/var/run")
    assert not sidecar_pty.TAKEOVER_TMUX_SOCKET.startswith(shared_prefixes)
    assert sidecar_pty.TAKEOVER_TMUX_SOCKET.startswith(sidecar_pty.TAKEOVER_TMUX_SOCKET_DIR)


@requires_tmux
def test_pty_reattach_preserves_shell_state_across_reconnect(monkeypatch):
    """The actual behavior issue #130 asks for: disconnect, then reconnect,
    and assert the same shell state (an exported env var) persists --
    proving the second connection reattached to the first's live tmux
    session instead of getting a brand-new shell. Uses an explicit -S
    socket path (issue #144), same as the real fixed build_pty_command."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    session_name = f"test-takeover-{uuid.uuid4().hex[:8]}"
    test_socket = _short_socket_path("reattach")
    # Absolute path, not bare "tmux": the real endpoint spawns this process
    # with env=SAFE_EXEC_ENV, whose PATH ("/usr/local/bin:/usr/bin:/bin") is
    # deliberately narrow and may not include wherever this test host's tmux
    # actually lives (e.g. Homebrew's /opt/homebrew/bin on Apple Silicon).
    monkeypatch.setattr(
        sidecar_main,
        "build_pty_command",
        lambda: [
            _TMUX_BIN, "-S", test_socket, "new-session", "-A", "-s", session_name,
            "/bin/bash", "--norc", "--noprofile",
        ],
    )
    client = _client()

    def _read_until(ws, needle: bytes, timeout: float = 10.0) -> bytes:
        collected = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = ws.receive_bytes()
            except WebSocketDisconnect:
                break
            collected += chunk
            if needle in collected:
                break
        return collected

    try:
        with client.websocket_connect("/pty", headers=_auth_headers()) as ws1:
            ws1.send_bytes(b"export MARKER_VAR=persisted_across_reconnect_42\n")
            ws1.send_bytes(b"echo READY1_$MARKER_VAR\n")
            first_output = _read_until(ws1, b"READY1_persisted_across_reconnect_42")
            assert b"READY1_persisted_across_reconnect_42" in first_output
        # `with` block exit disconnects ws1 -- the finally block in
        # pty_takeover terminates this connection's tmux *client*, but the
        # tmux *server* (and the session/shell it's running) must survive.

        with client.websocket_connect("/pty", headers=_auth_headers()) as ws2:
            ws2.send_bytes(b"echo READY2_$MARKER_VAR\n")
            second_output = _read_until(ws2, b"READY2_persisted_across_reconnect_42")
            assert b"READY2_persisted_across_reconnect_42" in second_output, (
                "reconnecting did not reattach to the same tmux session -- "
                "MARKER_VAR from the first connection was lost"
            )
    finally:
        subprocess.run(
            ["tmux", "-S", test_socket, "kill-session", "-t", session_name],
            capture_output=True,
        )


@requires_tmux
def test_pty_new_session_name_creates_fresh_session_with_no_state(monkeypatch):
    """Sanity check for the reattach test above: a session name that has
    never been used before must NOT see another test's state -- `-A` only
    reattaches to a session of the SAME name, it doesn't share state
    globally."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    session_name = f"test-takeover-fresh-{uuid.uuid4().hex[:8]}"
    test_socket = _short_socket_path("fresh")
    monkeypatch.setattr(
        sidecar_main,
        "build_pty_command",
        lambda: [
            _TMUX_BIN, "-S", test_socket, "new-session", "-A", "-s", session_name,
            "/bin/bash", "--norc", "--noprofile",
        ],
    )
    client = _client()

    def _read_until(ws, needle: bytes, timeout: float = 10.0) -> bytes:
        collected = b""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = ws.receive_bytes()
            except WebSocketDisconnect:
                break
            collected += chunk
            if needle in collected:
                break
        return collected

    try:
        with client.websocket_connect("/pty", headers=_auth_headers()) as ws:
            ws.send_bytes(b"echo VAR_IS[$MARKER_VAR]\n")
            output = _read_until(ws, b"VAR_IS[]")
            assert b"VAR_IS[]" in output
    finally:
        subprocess.run(
            ["tmux", "-S", test_socket, "kill-session", "-t", session_name],
            capture_output=True,
        )


@requires_tmux
def test_takeover_tmux_socket_is_unreachable_via_default_tmux_socket_resolution():
    """Empirical proof for GitHub issue #144's fix: a process that invokes
    plain `tmux` with NO `-S` flag -- exactly what any process nsentered/
    docker-exec'd into the sandbox's own namespace as the sandbox UID would
    be limited to, since it has no way to learn a socket path living
    outside every volume it can see -- must NOT be able to see or attach
    to a takeover session living on an explicit, non-default socket path.

    This doesn't spin up real separate containers (none exist in this unit
    test environment), but it exercises the actual mechanism the fix relies
    on: tmux session visibility is scoped by socket path, not merely by
    UID. That is precisely what confines the takeover session once its
    socket lives at TAKEOVER_TMUX_SOCKET (/run/boxkite/...) -- a path
    neither deploy/pod-template.yaml nor deploy/docker-compose.yml mounts
    into the sandbox container at all, unlike /tmp (see
    test_takeover_tmux_socket_path_is_outside_every_sandbox_shared_volume
    above for the static guard on the path itself).
    """
    explicit_socket = _short_socket_path("isolation")
    session_name = f"test-isolation-{uuid.uuid4().hex[:8]}"

    create = subprocess.run(
        [_TMUX_BIN, "-S", explicit_socket, "new-session", "-d", "-s", session_name, "sleep 30"],
        capture_output=True,
        text=True,
    )
    assert create.returncode == 0, create.stderr

    try:
        # Sanity check: the session genuinely exists on the explicit socket.
        confirm = subprocess.run(
            [_TMUX_BIN, "-S", explicit_socket, "has-session", "-t", session_name],
            capture_output=True,
        )
        assert confirm.returncode == 0

        # The actual proof: plain `tmux` (default socket resolution, no -S)
        # -- what a same-UID process with no knowledge of explicit_socket
        # would run -- cannot see this session at all.
        default_socket_attempt = subprocess.run(
            [_TMUX_BIN, "has-session", "-t", session_name],
            capture_output=True,
            text=True,
        )
        assert default_socket_attempt.returncode != 0, (
            "a plain `tmux has-session` with no -S flag (the default "
            "socket resolution a same-UID sandboxed process would be "
            "limited to) unexpectedly found the takeover session -- "
            "isolation is broken"
        )

        default_list_attempt = subprocess.run(
            [_TMUX_BIN, "list-sessions"], capture_output=True, text=True
        )
        assert session_name not in default_list_attempt.stdout
    finally:
        subprocess.run(
            [_TMUX_BIN, "-S", explicit_socket, "kill-session", "-t", session_name],
            capture_output=True,
        )


@requires_tmux
def test_takeover_tmux_never_reads_a_config_file(monkeypatch, tmp_path):
    """SECURITY regression test: tmux now runs as the sidecar's own process
    (the #144 fix), inheriting SAFE_EXEC_ENV's HOME=/workspace /
    XDG_CONFIG_HOME=/tmp/.config -- both sandbox-writable volumes. Without
    `-f /dev/null`, tmux parses $XDG_CONFIG_HOME/tmux/tmux.conf or
    ~/.tmux.conf at server start and executes any `run-shell` directive
    immediately, no operator interaction required -- a sandboxed agent
    planting a poisoned config at either path would get code execution as
    the sidecar the next time an operator opens takeover. This proves
    build_pty_command's actual argv includes `-f /dev/null` AND that a
    planted config genuinely does not execute when tmux is invoked with it.
    """
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "k8s")
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 4242)
    monkeypatch.setattr(sidecar_main, "SANDBOX_UID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_GID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED", False)
    monkeypatch.setattr(sidecar_pty, "_ensure_takeover_tmux_socket_dir", lambda: None)

    cmd = sidecar_pty.build_pty_command()
    assert cmd[0] == "tmux"
    assert "-f" in cmd
    f_index = cmd.index("-f")
    assert cmd[f_index + 1] == "/dev/null"
    # -f must appear before the "--" separator, i.e. apply to tmux itself,
    # not be swallowed as an argument to the wrapped entry command.
    assert f_index < cmd.index("--")

    # Empirical proof, not just an argv assertion: plant a real payload in
    # both candidate config locations and confirm invoking tmux exactly the
    # way build_pty_command does (-f /dev/null -S <socket> new-session ...)
    # does not execute it.
    fake_home = tmp_path / "workspace"
    fake_xdg_config = tmp_path / "tmp" / ".config"
    (fake_home).mkdir()
    (fake_xdg_config / "tmux").mkdir(parents=True)
    proof_file = tmp_path / "pwned"
    payload = f'run-shell "touch {proof_file}"\n'
    (fake_home / ".tmux.conf").write_text(payload)
    (fake_xdg_config / "tmux" / "tmux.conf").write_text(payload)

    test_socket = _short_socket_path("no-config-exec")
    session_name = f"test-no-config-exec-{uuid.uuid4().hex[:8]}"
    env = {
        "HOME": str(fake_home),
        "XDG_CONFIG_HOME": str(fake_xdg_config),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
    }
    try:
        subprocess.run(
            [_TMUX_BIN, "-f", "/dev/null", "-S", test_socket,
             "new-session", "-d", "-s", session_name, "sleep 2"],
            env=env, capture_output=True, timeout=10,
        )
        time.sleep(0.5)
        assert not proof_file.exists(), (
            "tmux executed a planted tmux.conf's run-shell directive -- "
            "-f /dev/null did not suppress config-file parsing as expected"
        )
    finally:
        subprocess.run(
            [_TMUX_BIN, "-S", test_socket, "kill-session", "-t", session_name],
            capture_output=True,
        )


def test_build_sandbox_entry_argv_skip_network_isolation_omits_unshare(monkeypatch):
    """GitHub issue #184: `_build_sandbox_entry_argv(..., skip_network_isolation=True)`
    must omit the `unshare -n` wrapper even when
    SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED is on -- sidecar_desktop.py's
    Xvfb/WM/x11vnc stack must share the pod's normal network namespace, not
    get its own private/unconnected one (see sidecar_desktop.py's module
    docstring)."""
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "k8s")
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 4242)
    monkeypatch.setattr(sidecar_main, "SANDBOX_UID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_GID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED", True)

    cmd = sidecar_pty._build_sandbox_entry_argv(["echo", "hi"], skip_network_isolation=True)

    assert cmd[0] != "unshare"
    assert cmd[0] == "nsenter"


def test_build_sandbox_entry_argv_default_unchanged_with_isolation_enabled(monkeypatch):
    """Regression guard: every existing caller (the default
    `skip_network_isolation=False`) must keep getting the `unshare -n`
    wrapper exactly as before this parameter was added."""
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "k8s")
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 4242)
    monkeypatch.setattr(sidecar_main, "SANDBOX_UID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_GID", 1001)
    monkeypatch.setattr(sidecar_main, "SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED", True)

    cmd = sidecar_pty._build_sandbox_entry_argv(["echo", "hi"])

    assert cmd[0] == "unshare"
    assert "nsenter" in cmd


def test_kill_takeover_tmux_session_runs_tmux_directly_on_explicit_socket(monkeypatch):
    """GitHub issue #144 fix: kill_takeover_tmux_session must run tmux
    DIRECTLY as the sidecar's own process, on the explicit socket path --
    never via exec_in_sandbox/nsenter/docker-exec, since the socket no
    longer lives inside the sandbox's own filesystem at all."""
    calls = []

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return b"", b""

    async def _fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        return _FakeProc()

    async def _unexpected_exec_in_sandbox(*args, **kwargs):
        raise AssertionError(
            "kill_takeover_tmux_session must not go through exec_in_sandbox "
            "-- the socket lives in the sidecar's own filesystem now"
        )

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _unexpected_exec_in_sandbox)
    monkeypatch.setattr(sidecar_pty.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(sidecar_pty, "_ensure_takeover_tmux_socket_dir", lambda: None)

    asyncio.run(sidecar_pty.kill_takeover_tmux_session())

    assert calls == [
        ("tmux", "-f", "/dev/null", "-S", sidecar_pty.TAKEOVER_TMUX_SOCKET, "kill-session", "-t", "takeover"),
    ]


def test_kill_takeover_tmux_session_tolerates_no_existing_session(monkeypatch):
    """The common case -- no takeover session was ever started on this pod --
    must not raise even though `tmux kill-session` exits non-zero."""

    class _FakeProc:
        returncode = 1

        async def communicate(self):
            return b"", b"can't find session: takeover"

    async def _fake_create_subprocess_exec(*args, **kwargs):
        return _FakeProc()

    monkeypatch.setattr(sidecar_pty.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)
    monkeypatch.setattr(sidecar_pty, "_ensure_takeover_tmux_socket_dir", lambda: None)

    asyncio.run(sidecar_pty.kill_takeover_tmux_session())


def test_configure_kills_takeover_tmux_session_before_wiping_session(monkeypatch, tmp_path):
    """Regression test for the cross-tenant leak this feature would
    otherwise introduce into pod recycling: /configure must kill the
    takeover tmux session so a recycled pod never hands a new tenant a
    still-live shell left behind by a previous tenant's takeover session."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
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

    # A fake, not the real function -- the real function's own behavior
    # (issuing `tmux kill-session -t takeover` via exec_in_sandbox) is
    # covered directly by test_kill_takeover_tmux_session_runs_tmux_kill_session
    # above; running it for real here would shell out to a real local tmux
    # on whatever machine runs this test suite, which could kill an
    # unrelated real session literally named "takeover" on a developer's
    # own machine -- not something a unit test should risk.
    calls = []

    async def _fake_kill_takeover():
        calls.append(True)

    monkeypatch.setattr(sidecar_main, "kill_takeover_tmux_session", _fake_kill_takeover)

    # Deliberately NOT `with TestClient(...) as client:` here: entering/
    # exiting the app's lifespan (startup/shutdown events) is unrelated to
    # what this test checks (that /configure calls kill_takeover_tmux_session)
    # and, on this Python/asyncio version, running that full lifespan cycle
    # back-to-back with certain other test files' own event-loop-bound
    # background tasks (_periodic_sync_task) is a pre-existing source of
    # cross-test flakiness independent of this feature -- reproducible even
    # against the analogous pre-existing interpreter test
    # (test_configure_kills_live_interpreter_before_wiping_session) with no
    # tmux-related code involved at all. A plain (non-context-manager)
    # TestClient still serves /configure correctly without ever starting
    # that background task.
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

    assert calls, "kill_takeover_tmux_session was not called by /configure"
