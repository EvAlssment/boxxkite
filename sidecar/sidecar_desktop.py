"""GUI/remote-desktop human takeover (WS /desktop) -- GitHub issue #184,
docs/GUI-COMPUTER-USE-SCOPING.md.

Bounded first slice: a human operator can open a URL and see/drive a real
GUI desktop inside a sandbox pod, the same way `WS /pty` already gives human
shell takeover (sidecar_pty.py). Agent-programmatic GUI tool calls (mouse/
keyboard/screenshot as callable tools -- the actual "Computer Use" half of
issue #184's title) are explicitly out of scope; see the scoping doc.

Mirrors sidecar_pty.py's/sidecar_browser.py's shape: one lazily-started,
kept-alive process group per session (Xvfb -> a window manager -> x11vnc),
bridged to a WebSocket. Gated by BOXKITE_DESKTOP_ENABLED (off by default,
same "flagged off until a security review" posture as
BOXKITE_AGENT_PTY_ENABLED/BOXKITE_BROWSER_ENABLED).

SECURITY (docs/GUI-COMPUTER-USE-SCOPING.md): unlike every other sidecar-
launched subprocess, this stack always enters the sandbox namespace via
`_build_sandbox_entry_argv(..., skip_network_isolation=True)` --
`unshare -n` would give x11vnc its own private, unconnected network
namespace, and the WS<->VNC bridge below (which connects to
127.0.0.1:DESKTOP_VNC_PORT from the SIDECAR's own network namespace) would
then have nothing to reach. This does NOT widen the pod's own NetworkPolicy
egress/ingress posture; it only means GUI apps a human runs inside this
session share the pod's normal network path, not a per-invocation isolated
netns -- a real, disclosed difference from the rest of the exec model (see
SECURITY.md's "remote desktop takeover" section).

There is no VNC-protocol-level authentication (`-nopw`): access control is
entirely the sidecar shared-secret + control-plane RBAC/token layer, the
same trust model PTY takeover already has (no second auth layer inside
tmux itself).
"""

import asyncio
import logging
import os
from typing import Optional

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

import main
from sidecar_pty import _build_sandbox_entry_argv, _check_pty_auth

logger = logging.getLogger("sidecar")

router = APIRouter()

DESKTOP_DISPLAY = ":1"
DESKTOP_VNC_PORT = 5901
DESKTOP_VNC_HOST = "127.0.0.1"
DESKTOP_SCREEN_GEOMETRY = "1280x800x24"

# Same X11 socket directory Xvfb creates on startup -- polled below to
# detect Xvfb actually coming up before spawning the window manager/x11vnc
# against a display that doesn't exist yet.
_X11_SOCKET_DIR = "/tmp/.X11-unix"
_X11_SOCKET_NAME = f"X{DESKTOP_DISPLAY.lstrip(':')}"

DESKTOP_STARTUP_TIMEOUT_SECONDS = max(
    1, int(os.environ.get("DESKTOP_STARTUP_TIMEOUT_SECONDS", "15"))
)
_DESKTOP_STARTUP_POLL_INTERVAL_SECONDS = 0.2

DESKTOP_READ_CHUNK_SIZE = 64 * 1024

# Tracked live process handles, keyed by stage name -- module-level so
# kill_desktop_session() (called from sidecar_sync.py's /configure, see its
# own docstring) can tear all three down without needing a session object
# threaded through from the WS handler.
_desktop_procs: dict[str, "asyncio.subprocess.Process"] = {}
# Stage spawn/teardown order matters: Xvfb must exist before the window
# manager or x11vnc can attach to its display, and teardown runs in reverse
# so nothing is left pointing at an already-killed display.
_DESKTOP_STAGE_ORDER = ("xvfb", "wm", "x11vnc")


def _build_desktop_entry_argv(argv: list[str]) -> Optional[list[str]]:
    """Enter the sandbox namespace for one desktop-stack process (Xvfb, the
    window manager, or x11vnc). Always skips the per-exec network
    isolation `unshare -n` wrapper -- see this module's docstring for why."""
    return _build_sandbox_entry_argv(argv, skip_network_isolation=True)


async def _spawn_desktop_stage(name: str, argv: list[str], *, extra_env: Optional[dict] = None) -> None:
    """Spawn one desktop-stack stage as a tracked background process."""
    entry_argv = _build_desktop_entry_argv(argv)
    if entry_argv is None:
        raise OSError("Failed to find sandbox process")

    env = dict(main.SAFE_EXEC_ENV)
    if extra_env:
        env.update(extra_env)

    proc = await asyncio.create_subprocess_exec(
        *entry_argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    _desktop_procs[name] = proc


async def _wait_for_x11_socket() -> None:
    """Poll for Xvfb's own control socket to appear under
    /tmp/.X11-unix -- Xvfb takes a moment to create it after spawning, and
    the window manager/x11vnc must not be started against a display that
    doesn't exist yet."""
    socket_path = os.path.join(_X11_SOCKET_DIR, _X11_SOCKET_NAME)
    deadline = asyncio.get_event_loop().time() + DESKTOP_STARTUP_TIMEOUT_SECONDS
    while asyncio.get_event_loop().time() < deadline:
        if os.path.exists(socket_path):
            return
        await asyncio.sleep(_DESKTOP_STARTUP_POLL_INTERVAL_SECONDS)
    raise OSError(
        f"Xvfb did not create {socket_path} within {DESKTOP_STARTUP_TIMEOUT_SECONDS}s"
    )


async def _wait_for_vnc_port() -> None:
    """Poll for x11vnc's VNC port to accept connections before returning
    control to the caller -- same bounded-retry shape as
    _wait_for_x11_socket above."""
    deadline = asyncio.get_event_loop().time() + DESKTOP_STARTUP_TIMEOUT_SECONDS
    last_error: Optional[Exception] = None
    while asyncio.get_event_loop().time() < deadline:
        try:
            _reader, writer = await asyncio.open_connection(DESKTOP_VNC_HOST, DESKTOP_VNC_PORT)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass
            return
        except OSError as e:
            last_error = e
            await asyncio.sleep(_DESKTOP_STARTUP_POLL_INTERVAL_SECONDS)
    raise OSError(
        f"x11vnc did not accept connections on {DESKTOP_VNC_HOST}:{DESKTOP_VNC_PORT} "
        f"within {DESKTOP_STARTUP_TIMEOUT_SECONDS}s ({last_error})"
    )


def _desktop_stack_is_live() -> bool:
    """Whether every tracked stage's process is still tracked and hasn't
    exited -- the idempotency check _ensure_desktop_stack_running uses to
    avoid respawning a stack that's already up."""
    if set(_desktop_procs.keys()) != set(_DESKTOP_STAGE_ORDER):
        return False
    return all(proc.returncode is None for proc in _desktop_procs.values())


async def _ensure_desktop_stack_running() -> None:
    """Idempotently ensure Xvfb -> a window manager -> x11vnc are all
    running, spawning whichever are missing. Raises OSError with a clear
    message if any stage fails to come up within its startup timeout --
    the caller (desktop_takeover below) turns that into a 1011 WS close,
    mirroring build_pty_command() returning None in sidecar_pty.py.
    """
    if _desktop_stack_is_live():
        return

    # A partially-up stack (e.g. a previous attempt failed midway) is torn
    # down and retried from scratch rather than patched in place -- simpler
    # and matches _get_or_spawn_browser_locked's own "not fresh -> kill and
    # respawn everything" discipline.
    if _desktop_procs:
        await kill_desktop_session()

    try:
        await _spawn_desktop_stage(
            "xvfb",
            ["Xvfb", DESKTOP_DISPLAY, "-screen", "0", DESKTOP_SCREEN_GEOMETRY, "-nolisten", "tcp"],
        )
        await _wait_for_x11_socket()

        await _spawn_desktop_stage("wm", ["fluxbox"], extra_env={"DISPLAY": DESKTOP_DISPLAY})

        await _spawn_desktop_stage(
            "x11vnc",
            [
                "x11vnc",
                "-display", DESKTOP_DISPLAY,
                "-rfbport", str(DESKTOP_VNC_PORT),
                "-listen", DESKTOP_VNC_HOST,
                "-nopw",
                "-shared",
                "-forever",
                "-noxdamage",
            ],
            extra_env={"DISPLAY": DESKTOP_DISPLAY},
        )
        await _wait_for_vnc_port()
    except OSError:
        await kill_desktop_session()
        raise


async def kill_desktop_session() -> None:
    """Tear down every tracked desktop-stack process, if any are running.

    SECURITY: this MUST run on every /configure call (pod recycle/claim),
    the same requirement kill_takeover_tmux_session/_reset_browser already
    have (sidecar_sync.py's configure(), GitHub issues #130/#144) -- a
    recycled pod must never hand a new tenant a still-live Xvfb/WM/x11vnc
    session (windows, clipboard, whatever the previous tenant had open)
    left over from before. Always call this, not just when
    BOXKITE_DESKTOP_ENABLED is set, since a still-live stack started while
    the flag was on must still be killed if the flag was since flipped off
    before this recycle.

    Reverse spawn order (x11vnc, then the window manager, then Xvfb) so
    nothing is left pointing at an already-torn-down display. Idempotent
    and swallows per-process teardown errors the same way
    kill_takeover_tmux_session/_kill_browser_handle do -- a stage that's
    already dead (or never existed) must not prevent tearing down the
    others.
    """
    for name in reversed(_DESKTOP_STAGE_ORDER):
        proc = _desktop_procs.pop(name, None)
        if proc is None:
            continue
        if proc.returncode is None:
            try:
                proc.kill()
                await asyncio.wait_for(proc.wait(), timeout=10)
            except Exception as e:
                logger.warning(f"[desktop] Failed to kill '{name}' stage: {e}")


def _check_desktop_auth(websocket: WebSocket) -> Optional[tuple[int, str]]:
    """Validate the shared-secret header on the WebSocket handshake
    request -- literally the same check as _check_pty_auth (see its
    docstring for the fail-closed/before-accept() reasoning)."""
    return _check_pty_auth(websocket)


async def _vnc_to_websocket(websocket: WebSocket, reader: asyncio.StreamReader) -> None:
    """Relay VNC server output to the client as binary WS frames until
    EOF/error. Unlike sidecar_pty.py's PTY equivalent, `asyncio.open_
    connection`'s StreamReader is already non-blocking-friendly -- no
    run_in_executor needed."""
    while True:
        try:
            data = await reader.read(DESKTOP_READ_CHUNK_SIZE)
        except OSError:
            break
        if not data:
            break
        try:
            await websocket.send_bytes(data)
        except (WebSocketDisconnect, RuntimeError):
            break


async def _websocket_to_vnc(websocket: WebSocket, writer: asyncio.StreamWriter) -> None:
    """Relay client WS frames to the VNC server's stdin until the socket
    closes."""
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
            writer.write(data)
            await writer.drain()
        except OSError:
            break


@router.websocket("/desktop")
async def desktop_takeover(websocket: WebSocket) -> None:
    """Interactive GUI desktop into the sandbox namespace for human
    takeover (GitHub issue #184). Lazily starts Xvfb/a window manager/
    x11vnc (see _ensure_desktop_stack_running), then bridges a raw TCP
    connection to x11vnc's own RFB port bidirectionally to this WebSocket
    until either side disconnects.

    Auth is validated BEFORE accept() -- see _check_desktop_auth, same
    reasoning as sidecar_pty.py's pty_takeover.

    Deliberate first-slice simplification vs. PTY takeover's tmux-based
    reattach model (docs/GUI-COMPUTER-USE-SCOPING.md): the desktop stack is
    torn down on every WS disconnect, not just on /configure. Reattach-to-
    a-live-desktop is real, valuable future work, but is deliberately not
    built speculatively here (YAGNI) -- it needs its own review once the
    base mechanism is proven.
    """
    if not main.BOXKITE_DESKTOP_ENABLED:
        await websocket.close(code=4404, reason="GUI desktop takeover is not enabled on this deployment.")
        return

    rejection = _check_desktop_auth(websocket)
    if rejection is not None:
        code, reason = rejection
        await websocket.close(code=code, reason=reason)
        return

    await websocket.accept()

    try:
        await _ensure_desktop_stack_running()
    except OSError as e:
        await websocket.close(code=1011, reason=str(e))
        return

    reader: Optional[asyncio.StreamReader] = None
    writer: Optional[asyncio.StreamWriter] = None
    reader_task: Optional[asyncio.Task] = None
    writer_task: Optional[asyncio.Task] = None
    try:
        reader, writer = await asyncio.open_connection(DESKTOP_VNC_HOST, DESKTOP_VNC_PORT)

        reader_task = asyncio.ensure_future(_vnc_to_websocket(websocket, reader))
        writer_task = asyncio.ensure_future(_websocket_to_vnc(websocket, writer))
        await asyncio.wait({reader_task, writer_task}, return_when=asyncio.FIRST_COMPLETED)
    except OSError as e:
        await websocket.close(code=1011, reason=f"Failed to connect to VNC server: {e}")
        return
    finally:
        for task in (reader_task, writer_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (reader_task, writer_task):
            if task is not None:
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
        if writer is not None:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception:
                pass
        try:
            await websocket.close()
        except RuntimeError:
            pass
        # First-slice scope decision (see module docstring): tear the
        # desktop stack down on every disconnect, not just on /configure.
        await kill_desktop_session()
