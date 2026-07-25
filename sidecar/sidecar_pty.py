"""PTY-backed routes: the human-takeover WS /pty endpoint and the
agent-callable /pty-exec endpoint (docs/AGENT-PTY-DESIGN.md,
docs/SANDBOX-OBSERVABILITY-DESIGN.md).

Split out of the original monolithic ``main.py`` (GitHub issue #71) as a pure
mechanical refactor -- no behavior change. Shared config/state/models remain
owned by ``main``; patched functions (``build_pty_command``, ``get_sandbox_pid``)
are called via ``main.`` so monkeypatching is observed.
"""

import asyncio
import base64
import hmac
import logging
import os
import pty
import select
import shlex
import subprocess
import time as _time
from typing import Optional

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

import main

logger = logging.getLogger("sidecar")

router = APIRouter()


# Interactive shell used for the /pty takeover endpoint. Same image, same
# user (adduser -D -u 1001 -s /bin/bash sandbox in deploy/sandbox.Dockerfile)
# that already makes /bin/bash the sandbox user's own login shell.
PTY_SHELL = "/bin/bash"

# GitHub issue #130: the /pty takeover endpoint spawns a persistent tmux
# session instead of a bare shell so a dropped WebSocket reattaches to the
# same live session instead of losing it. tmux's client/server split is what
# makes this work with no extra plumbing: the session (and the real shell
# running inside it) lives in a detached tmux server process; the thing
# this module's Popen/PTY pair actually talks to is a tmux *client*, and
# killing a client (however that happens -- clean disconnect, network drop,
# `finally`'s proc.terminate()) only detaches it. `-A` (attach-or-create)
# means the first connection ever creates the session and every connection
# after that reattaches to it. A single fixed session name is deliberate:
# one takeover session per sandbox pod, matching this feature's existing
# single-session scope (see GitHub issue #132 for concurrent multi-client
# attach to this same session).
#
# GitHub issue #144 (CRITICAL, found before merge): the first pass of this
# feature ran tmux itself *inside* the sandbox's own mount/PID namespace, as
# the sandbox UID (i.e. nsenter/docker-exec wrapped tmux, the same way they
# wrap every other sandboxed command). Its control socket then landed under
# /tmp -- which deploy/pod-template.yaml and deploy/docker-compose.yml both
# mount as the SAME shared volume between the sidecar and sandbox
# containers -- so any process with the sandbox UID (i.e. any bash_tool/
# /exec command, since those are nsentered into the exact same namespace/
# UID) could reach that socket directly (`tmux attach`, `send-keys`,
# `capture-pane`) with zero enumeration, since "takeover" is a fixed,
# publicly-known session name in this OSS repo. That defeats the entire
# point of a human-takeover channel: a compromised/prompt-injected agent
# could observe or hijack the operator's own supervisory session.
#
# THE FIX: invert the wrapping order. tmux now runs as the SIDECAR's own
# process (never nsentered into the sandbox), on an explicit socket path
# outside every volume shared with the sandbox container (neither container
# mounts /run/boxkite -- see deploy/pod-template.yaml's and
# deploy/docker-compose.yml's volume lists). The nsenter invocation that
# actually enters the sandbox becomes tmux's OWN pane
# command instead of the other way around -- see build_pty_command below.
# This relies on the sidecar container having its own private, writable
# container-layer filesystem: it does NOT set readOnlyRootFilesystem (see
# deploy/pod-template.yaml's sidecar securityContext), so /run/boxkite is
# genuinely unreachable from the sandbox container/namespace, not merely
# permission-gated.
TAKEOVER_TMUX_SESSION = "takeover"
TAKEOVER_TMUX_SOCKET_DIR = "/run/boxkite"
TAKEOVER_TMUX_SOCKET = f"{TAKEOVER_TMUX_SOCKET_DIR}/takeover.sock"


def _ensure_takeover_tmux_socket_dir() -> None:
    """Create the sidecar-private directory that holds the takeover tmux
    control socket, if it doesn't already exist. Idempotent and cheap
    enough to call before every tmux invocation rather than depend on a
    one-time startup hook running first.

    Deliberately swallows OSError (e.g. a read-only filesystem at that
    path -- true of some local/dev/test environments that have no real
    `/run`, never true of the actual sidecar container image, which builds
    this directory in and runs with its own writable layer): failing to
    create this directory should degrade the takeover feature (the
    subsequent tmux invocation itself fails, surfaced to the operator's own
    WS connection as a close/error), not take down /configure's pod-recycle
    teardown path for every other tenant-isolation concern it also handles
    (killing background processes, resetting interpreters).
    """
    try:
        os.makedirs(TAKEOVER_TMUX_SOCKET_DIR, exist_ok=True)
    except OSError as e:
        logger.warning(f"[pty] could not create {TAKEOVER_TMUX_SOCKET_DIR}: {e}")


def _build_sandbox_entry_argv(
    argv: list[str], *, skip_network_isolation: bool = False
) -> Optional[list[str]]:
    """Build the nsenter argv that enters the sandbox container's own
    mount+PID namespace as the sandbox UID and execs `argv` there. Mirrors
    exec_in_sandbox/build_k8s_exec_command's namespace-entry mechanism
    exactly (same nsenter/unshare flags, same UID drop, in both runtime
    modes). Returns None when the sandbox process can't be found, same
    failure signal exec_in_sandbox uses.

    Shared by both callers of build_pty_command below: `/pty-exec`'s
    one-shot `exec_argv` runs this directly with no PTY-route-specific
    wrapping, while the takeover route's persistent shell wraps this
    result in the tmux invocation described above (GitHub issue #144) --
    entering the sandbox is always the INNERMOST command, never the
    outermost one, so tmux itself is never sandboxed.

    `skip_network_isolation` (GitHub issue #184, docs/GUI-COMPUTER-USE-
    SCOPING.md): the same narrow, scoped override
    `build_k8s_exec_command`'s own `skip_network_isolation` param already
    gives `/process/start`'s `expose_port` path and `sidecar_browser.py`'s
    driver spawn -- used ONLY by sidecar_desktop.py's Xvfb/WM/x11vnc
    stack, which must share the pod's normal network namespace (not an
    empty per-exec one) since a human takes over this session over VNC,
    which needs the pod's own network path to be reachable at all. Default
    `False` preserves every existing caller/test byte-for-byte.
    """
    sandbox_pid = main.get_sandbox_pid()
    if not sandbox_pid:
        return None

    nsenter_cmd = [
        "nsenter",
        "-t", str(sandbox_pid),
        "-m", "-p",  # Mount and PID namespaces
        "--setuid", str(main.SANDBOX_UID),  # SECURITY: Drop to sandbox user
        "--setgid", str(main.SANDBOX_GID),  # SECURITY: Drop to sandbox group
        "--", *argv,
    ]

    if skip_network_isolation or not main.SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED:
        return nsenter_cmd

    # SECURITY: same as build_k8s_exec_command -- unshare must run before
    # nsenter enters the sandbox mount namespace. tmux itself needs no
    # network isolation (it never touches the network) -- it's the shell
    # entered via this nsenter/unshare argv that does.
    return ["unshare", "-n", *nsenter_cmd]


def build_pty_command(exec_argv: Optional[list[str]] = None) -> Optional[list[str]]:
    """Build the command for a PTY-backed route -- either the /pty
    takeover endpoint's persistent tmux session (default, `exec_argv=None`;
    see GitHub issues #130/#144), or (docs/AGENT-PTY-DESIGN.md)
    `/pty-exec`'s specific one-shot command via `exec_argv`.

    SECURITY (docs/AGENT-PTY-DESIGN.md §3): `exec_argv`, when given, is
    the caller's own argv list, never a shell an agent could then use to
    chain arbitrary follow-up commands invisible to the tool-calling
    loop's own audit trail -- same reasoning bash_tool's one-shot `sh -c
    <command>` already follows. It is NOT run through `sh -c`, so shell
    metacharacters in it are inert, not a second injection surface. This
    path is unaffected by tmux entirely -- it goes straight through
    _build_sandbox_entry_argv, same as before issue #130.

    Returns None when the sandbox process can't be found (K8s mode only),
    same failure signal exec_in_sandbox uses.
    """
    if exec_argv is not None:
        return _build_sandbox_entry_argv(exec_argv)

    # Takeover route: tmux runs as the sidecar's own process, on an
    # explicit socket outside every volume shared with the sandbox
    # container -- see the module-level comment above (GitHub issue #144)
    # for why. The nsenter entry into the sandbox is tmux's own pane
    # command (`--` then the entry argv), not the other way around.
    entry_argv = _build_sandbox_entry_argv([PTY_SHELL])
    if entry_argv is None:
        return None

    _ensure_takeover_tmux_socket_dir()
    return [
        # SECURITY: -f /dev/null is load-bearing, not cosmetic. tmux now
        # runs as the sidecar's own (elevated) process, inheriting
        # SAFE_EXEC_ENV's HOME=/workspace / XDG_CONFIG_HOME=/tmp/.config --
        # both sandbox-writable volumes. Without -f /dev/null, tmux parses
        # $XDG_CONFIG_HOME/tmux/tmux.conf or ~/.tmux.conf at server start and
        # executes any `run-shell` directive in it immediately, no operator
        # interaction required -- a sandboxed agent planting one of those
        # files (ordinary write access to /workspace or /tmp) would get code
        # execution as the sidecar the next time an operator opens takeover.
        # See tests/test_sidecar_pty.py's
        # test_takeover_tmux_never_reads_a_config_file for the regression
        # test proving this.
        "tmux", "-f", "/dev/null", "-S", TAKEOVER_TMUX_SOCKET,
        "new-session", "-A", "-s", TAKEOVER_TMUX_SESSION,
        "--", *entry_argv,
    ]


async def kill_takeover_tmux_session() -> None:
    """Tear down the persistent takeover tmux session, if one exists.

    SECURITY: this MUST run on every /configure call (pod recycle/claim),
    the same requirement _kill_all_processes()/_reset_interpreter()/
    _reset_node_interpreter() already have, for the same reason: issue
    #130 makes the takeover shell survive a dropped WebSocket by design,
    which means it would just as happily survive a pod recycle into a
    *different tenant's* session if nothing killed it explicitly --
    handing that new tenant a live shell (env vars, cwd, shell history,
    anything still running) left behind by whoever last used takeover on
    this pod.

    Unlike the first (broken, issue #144) implementation, this does NOT go
    through exec_in_sandbox/nsenter/docker-exec: the tmux socket now lives
    in the SIDECAR's own filesystem (see TAKEOVER_TMUX_SOCKET above), not
    the sandbox's, so tearing it down means running `tmux kill-session`
    directly as the sidecar process, on the same explicit socket path.

    `tmux kill-session` exits non-zero when no server/session exists at
    all -- the common case (a pod recycled without takeover ever having
    been used, or before /run/boxkite has ever been created) -- so a
    non-zero exit (including "no such file or directory" for the socket
    itself) is expected here, not logged as an error.
    """
    _ensure_takeover_tmux_socket_dir()
    proc = await asyncio.create_subprocess_exec(
        # SECURITY: -f /dev/null for the same reason build_pty_command's
        # tmux invocation needs it -- belt-and-suspenders in case this ever
        # runs when no server is up yet (kill-session alone shouldn't spawn
        # one, but don't rely on that tmux-version-dependent behavior).
        "tmux", "-f", "/dev/null", "-S", TAKEOVER_TMUX_SOCKET,
        "kill-session", "-t", TAKEOVER_TMUX_SESSION,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.debug(
            f"[pty] tmux -S {TAKEOVER_TMUX_SOCKET} kill-session -t {TAKEOVER_TMUX_SESSION}: "
            f"exit={proc.returncode} ({stderr.decode(errors='replace').strip() or 'no session/server'})"
        )


PTY_READ_CHUNK_SIZE = 64 * 1024


def _check_pty_auth(websocket: WebSocket) -> Optional[tuple[int, str]]:
    """Validate the shared-secret header on the WebSocket handshake request.

    Returns (close_code, reason) if the connection must be rejected, or None
    if it's authorized. Must be called and acted on BEFORE websocket.accept()
    -- see the SIDECAR_AUTH_TOKEN comment block and docs/SANDBOX-OBSERVABILITY-DESIGN.md
    §4 for why an unauthenticated upgrade that's rejected only after the PTY
    is allocated is unacceptable (wasted allocation, briefly-live shell).

    Same fail-closed semantics as enforce_sidecar_auth: no token configured
    at all -> reject, not silently open.
    """
    if not main.SIDECAR_AUTH_TOKEN:
        return 1013, "Sidecar auth is not configured (SIDECAR_AUTH_TOKEN is unset)"

    supplied = websocket.headers.get(main.SIDECAR_AUTH_HEADER, "")
    if not supplied or not hmac.compare_digest(supplied, main.SIDECAR_AUTH_TOKEN):
        return 4401, "Missing or invalid sidecar auth token"

    return None


async def _pty_to_websocket(websocket: WebSocket, master_fd: int) -> None:
    """Relay PTY output to the client as binary WS frames until EOF/error."""
    loop = asyncio.get_event_loop()
    while True:
        try:
            data = await loop.run_in_executor(None, os.read, master_fd, PTY_READ_CHUNK_SIZE)
        except OSError:
            # EIO is the expected signal that the child exited and the PTY's
            # slave side has been closed -- not an error worth logging loudly.
            break
        if not data:
            break
        try:
            await websocket.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError):
            break


async def _websocket_to_pty(websocket: WebSocket, master_fd: int) -> None:
    """Relay client WS frames to the PTY's stdin until the socket closes."""
    while True:
        try:
            message = await websocket.receive()
        except WebSocketDisconnect:
            break
        if message.get("type") == "websocket.disconnect":
            break
        data = message.get("bytes")
        if data is None:
            text = message.get("text")
            data = text.encode("utf-8") if text is not None else None
        if not data:
            continue
        try:
            os.write(master_fd, data)
        except OSError:
            break


@router.websocket("/pty")
async def pty_takeover(websocket: WebSocket) -> None:
    """Interactive PTY into the sandbox namespace for human takeover.

    Allocates a PTY via the pty stdlib module, execs a persistent tmux
    session -- running as the sidecar's own process, on a socket outside
    every sandbox-shared volume (see build_pty_command, GitHub issues
    #130/#144) -- whose own pane command enters the sandbox's namespace
    using the same nsenter (K8s) / docker exec (compose) mechanism
    exec_in_sandbox uses, and bridges the PTY fd to this WebSocket
    bidirectionally until either side disconnects.

    Auth is validated BEFORE accept() -- see _check_pty_auth. Every other
    sidecar route is protected by the enforce_sidecar_auth HTTP middleware,
    but ASGI HTTP middleware does not run for WebSocket connections, so this
    endpoint re-implements the same check explicitly.

    Disconnect no longer ends the session (issue #130): the `finally` block
    below still unconditionally terminates `proc`, but `proc` is now only
    the tmux *client* attached via `-A`, not the shell itself -- tmux's own
    server process (forked off, detached, on first connect) keeps the real
    shell alive independent of any one client's lifetime, so terminating the
    client here just detaches it. The next `WS /pty` connection spawns a new
    client that reattaches via the same `-A` flag and picks the same session
    back up. See kill_takeover_tmux_session below for why this session must
    still be torn down explicitly on pod recycle.
    """
    rejection = _check_pty_auth(websocket)
    if rejection is not None:
        code, reason = rejection
        await websocket.close(code=code, reason=reason)
        return

    await websocket.accept()

    cmd = main.build_pty_command()
    if cmd is None:
        await websocket.close(code=1011, reason="Failed to find sandbox process")
        return

    master_fd, slave_fd = pty.openpty()
    proc: Optional[subprocess.Popen] = None
    reader_task: Optional[asyncio.Task] = None
    writer_task: Optional[asyncio.Task] = None
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=dict(main.SAFE_EXEC_ENV),
            start_new_session=True,
        )
        # The child now holds its own copy of the slave fd; the sidecar's
        # copy must be closed or the child's stdout never sees EOF-on-exit.
        os.close(slave_fd)
        slave_fd = -1
        # master_fd is read via run_in_executor (a blocking thread-pool read),
        # so it must stay in blocking mode -- non-blocking here would make
        # os.read raise BlockingIOError (an OSError) the instant no data is
        # immediately available, which _pty_to_websocket would misread as
        # "child exited" and tear the session down after the first byte.

        reader_task = asyncio.ensure_future(_pty_to_websocket(websocket, master_fd))
        writer_task = asyncio.ensure_future(_websocket_to_pty(websocket, master_fd))
        await asyncio.wait({reader_task, writer_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        # Kill the child BEFORE cancelling/awaiting the tasks below. This
        # order matters: reader_task's blocking os.read(master_fd) (run in a
        # thread pool executor) only returns once every process holding the
        # PTY's slave side open -- i.e. the child -- has exited; awaiting it
        # first and killing the child second would deadlock on a child that
        # never produces output again after the client disconnects.
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)

        for task in (reader_task, writer_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (reader_task, writer_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass

        if slave_fd != -1:
            try:
                os.close(slave_fd)
            except OSError:
                pass
        try:
            os.close(master_fd)
        except OSError:
            pass
        try:
            await websocket.close()
        except RuntimeError:
            pass


# ============================================================================
# Agent-callable PTY (docs/AGENT-PTY-DESIGN.md, option A)
#
# A bounded, request/response PTY exec -- reuses build_pty_command/
# pty.openpty() from the human-takeover /pty route above, but execs the
# caller's own argv directly (never an interactive shell an agent could
# chain further commands into) and returns captured output once the
# process exits or `timeout_seconds` elapses, fitting the same
# request/response tool-calling contract every other boxkite tool has.
# Gated by BOXKITE_AGENT_PTY_ENABLED (off by default) -- this is new
# attack surface (a second PTY-allocation path, agent-reachable, not just
# an operator's own WS session), not a copy-paste of an already-reviewed
# feature.
# ============================================================================

PTY_EXEC_MAX_OUTPUT_BYTES = 256 * 1024
PTY_EXEC_MAX_TIMEOUT_SECONDS = 120


async def _pty_exec_once(argv: list[str], input_data: bytes, timeout_seconds: float) -> tuple[bytes, Optional[int], bool]:
    """Blocking PTY allocate/exec/read/teardown, run in a thread so the
    event loop isn't blocked on the blocking os.read/select calls."""

    def _blocking_pty_exec() -> tuple[bytes, Optional[int], bool]:
        cmd = main.build_pty_command(argv)
        if cmd is None:
            raise OSError("Failed to find sandbox process")

        master_fd, slave_fd = pty.openpty()
        proc: Optional[subprocess.Popen] = None
        output = bytearray()
        timed_out = False
        try:
            proc = subprocess.Popen(
                cmd,
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                env=dict(main.SAFE_EXEC_ENV),
                start_new_session=True,
            )
            os.close(slave_fd)
            slave_fd = -1

            if input_data:
                os.write(master_fd, input_data)

            deadline = _time.monotonic() + timeout_seconds
            while True:
                remaining = deadline - _time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    break
                readable, _, _ = select.select([master_fd], [], [], remaining)
                if master_fd not in readable:
                    timed_out = True
                    break
                try:
                    chunk = os.read(master_fd, 64 * 1024)
                except OSError:
                    break  # EIO -- child exited and closed its side of the PTY
                if not chunk:
                    break
                if len(output) < PTY_EXEC_MAX_OUTPUT_BYTES:
                    output.extend(chunk[: PTY_EXEC_MAX_OUTPUT_BYTES - len(output)])
                if proc.poll() is not None:
                    break

            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)

            return bytes(output), proc.returncode, timed_out
        finally:
            if slave_fd != -1:
                try:
                    os.close(slave_fd)
                except OSError:
                    pass
            try:
                os.close(master_fd)
            except OSError:
                pass

    return await asyncio.get_event_loop().run_in_executor(None, _blocking_pty_exec)


@router.post("/pty-exec", response_model=main.PtyExecResponse)
async def pty_exec(req: main.PtyExecRequest):
    """Run one command behind a real pseudo-terminal, bounded by
    `timeout_seconds`. See this module's own section docstring above and
    docs/AGENT-PTY-DESIGN.md for why this is a distinct route from the
    human-takeover WS /pty, not a variant of it.

    Session exec budget (GitHub issue #122): shares the exact same
    counters/sticky flag as /exec, /interpreter/exec, /process/start, and
    /node-interpreter/exec -- reserved (and count-checked) before the
    command runs, duration recorded (and seconds-checked) after it
    finishes.
    """
    if not main.BOXKITE_AGENT_PTY_ENABLED:
        raise HTTPException(status_code=404, detail="Agent-callable PTY is not enabled on this deployment.")

    if not req.command or not req.command.strip():
        raise HTTPException(status_code=400, detail="command is required")

    try:
        argv = shlex.split(req.command)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Could not parse command: {e}")
    if not argv:
        raise HTTPException(status_code=400, detail="command is required")

    try:
        input_data = base64.b64decode(req.input_bytes, validate=True) if req.input_bytes else b""
    except Exception:
        raise HTTPException(status_code=400, detail="input_bytes must be valid base64")

    await main._reserve_session_exec_slot_or_raise(source="pty_exec")

    timeout_seconds = min(max(0.1, req.timeout_seconds), PTY_EXEC_MAX_TIMEOUT_SECONDS)

    _t0 = _time.monotonic()
    error_to_raise: Optional[HTTPException] = None
    response: Optional["main.PtyExecResponse"] = None
    try:
        output, exit_code, timed_out = await _pty_exec_once(argv, input_data, timeout_seconds)
        response = main.PtyExecResponse(
            output=output.decode("utf-8", errors="replace"),
            exit_code=exit_code,
            timed_out=timed_out,
        )
    except OSError as e:
        logger.error(f"[pty-exec] {e}")
        error_to_raise = HTTPException(status_code=500, detail=str(e))

    duration_seconds = _time.monotonic() - _t0
    await main._record_session_exec_duration_or_raise(duration_seconds, source="pty_exec")

    if error_to_raise is not None:
        raise error_to_raise
    return response
