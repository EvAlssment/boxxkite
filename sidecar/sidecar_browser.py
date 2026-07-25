"""Headless Chromium browser automation (one kept-alive process per session)
and the /browser/* routes -- docs/BROWSER-EXEC-DESIGN.md, GitHub issue #119.

Mirrors sidecar_node_interpreter.py's shape exactly (see its module
docstring): ONE lazily-started driver subprocess kept alive for the whole
session, addressed by session id rather than an explicit handle the caller
has to remember, torn down on idle timeout / explicit close / session
recycle. The driver here is a small Node.js script that drives a real
headless Chromium process via Playwright (Playwright's own API sits on top
of CDP -- see docs/BROWSER-EXEC-DESIGN.md §2), communicating with this
sidecar over the same newline-delimited JSON request/response protocol
sidecar_node_interpreter.py's driver uses over stdin/stdout.

Gated by BOXKITE_BROWSER_ENABLED (off by default): new attack surface, and
per docs/BROWSER-EXEC-DESIGN.md §5, this one deserves MORE scrutiny than any
other opt-in tool this repo ships before being turned on for a real
multi-tenant deployment -- it is the first tool here whose egress needs
cannot be expressed as a fixed, enumerable host allowlist (§3).

SECURITY -- network isolation (docs/BROWSER-EXEC-DESIGN.md §3.1): unlike
every other sidecar-launched subprocess, the browser driver is spawned with
`skip_network_isolation=True` (K8s mode) -- the SAME narrow,
per-exec-only escape hatch /process/start's `expose_port` path already
uses, applied here ONLY to the browser driver's own spawn command, never
session-wide. Every other exec/interpreter/process call for this session
still gets the normal per-call empty network namespace. The corresponding
NetworkPolicy this requires lives in
src/boxkite/browser_network_policy.py, not in this file -- this module has
no say over what the browser process can actually reach once it has a
network namespace; only the operator's NetworkPolicy (or its absence) does.
"""

import asyncio
import base64
import json
import logging
import os
import time as _time
from datetime import datetime
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException

import main
from sidecar_interpreter import _INTERPRETER_READ_CHUNK_BYTES, _read_interpreter_response_line

logger = logging.getLogger("sidecar")

router = APIRouter()


_BROWSER_READY_SENTINEL = "__BOXKITE_BROWSER_READY__"

# One Node.js driver script, `require('playwright')`'d the same way
# NODE_PATH/PLAYWRIGHT_BROWSERS_PATH are already set up for the sandbox
# image's document-conversion skills (see docs/BROWSER-EXEC-DESIGN.md §1) --
# no new binary or image change needed. Deliberately never passes
# `executablePath` in production: Playwright resolves its own pinned,
# checksum-verified Chrome-for-Testing build from PLAYWRIGHT_BROWSERS_PATH
# on its own. BOXKITE_BROWSER_EXECUTABLE_PATH is an optional escape hatch
# (unset in production) so an operator -- or a test harness without the
# sandbox image's pinned browser available -- can point at a different
# Chromium/Chrome binary explicitly.
#
# One page per session (design doc §7 -- multiple tabs/contexts explicitly
# deferred): ensurePage() lazily (re)creates the page if it was never
# created or was closed out from under it, but there is never more than one
# live page at a time.
#
# Protocol per line: request {"action": "navigate"|"exec"|"screenshot",
# ...params} -> response {"data": {...}|null, "error": "<message>"|null}.
# There is no "close" action in this protocol -- /browser/close (see
# browser_close below) tears down the whole OS process instead of sending
# an in-band command, the same way /node-interpreter/reset kills its whole
# process rather than clearing state in-band. A call that times out (from
# this sidecar's own point of view) also kills the whole process and starts
# fresh next time -- same contract as the Python/Node interpreters.
_BROWSER_DRIVER_SOURCE_TEMPLATE = """
const { chromium } = require('playwright');
const readline = require('readline');

let browser = null;
let page = null;

async function ensureBrowser() {
  if (browser === null) {
    const launchOptions = { headless: true };
    if (process.env.BOXKITE_BROWSER_EXECUTABLE_PATH) {
      launchOptions.executablePath = process.env.BOXKITE_BROWSER_EXECUTABLE_PATH;
    }
    browser = await chromium.launch(launchOptions);
  }
  return browser;
}

async function ensurePage() {
  const b = await ensureBrowser();
  if (page === null || page.isClosed()) {
    page = await b.newPage();
  }
  return page;
}

async function handleNavigate(req) {
  const p = await ensurePage();
  const response = await p.goto(req.url, {
    waitUntil: req.wait_until || 'load',
    timeout: req.timeout_ms,
  });
  return {
    title: await p.title(),
    url: p.url(),
    status: response ? response.status() : null,
  };
}

async function handleExec(req) {
  const p = await ensurePage();
  const result = await p.evaluate(req.script);
  return { result: result === undefined ? null : result };
}

async function handleScreenshot(req) {
  const p = await ensurePage();
  const buf = await p.screenshot({ fullPage: !!req.full_page, type: 'png' });
  if (req.max_bytes && buf.length > req.max_bytes) {
    throw new Error(
      'Screenshot is ' + buf.length + ' bytes, exceeding the ' + req.max_bytes +
      '-byte cap; try full_page=false or a smaller viewport'
    );
  }
  return { image_base64: buf.toString('base64') };
}

async function dispatch(req) {
  switch (req.action) {
    case 'navigate': return await handleNavigate(req);
    case 'exec': return await handleExec(req);
    case 'screenshot': return await handleScreenshot(req);
    default: throw new Error('Unknown action: ' + req.action);
  }
}

process.stdout.write("BOXKITE_READY_SENTINEL\\n");

const rl = readline.createInterface({ input: process.stdin, terminal: false });
// Requests are processed strictly one at a time, chained onto this promise
// -- the sidecar's own asyncio.Lock already ensures only one call is ever
// in flight, this is just defense in depth against any future caller that
// doesn't honor that.
let chain = Promise.resolve();
rl.on('line', (rawLine) => {
  const line = rawLine.trim();
  if (!line) return;
  chain = chain.then(async () => {
    let req;
    try {
      req = JSON.parse(line);
    } catch (e) {
      process.stdout.write(JSON.stringify({ data: null, error: 'Invalid request: ' + e }) + '\\n');
      return;
    }
    try {
      const data = await dispatch(req);
      process.stdout.write(JSON.stringify({ data, error: null }) + '\\n');
    } catch (e) {
      const message = (e && e.message) ? String(e.message) : String(e);
      process.stdout.write(JSON.stringify({ data: null, error: message }) + '\\n');
    }
  });
});
"""

_BROWSER_DRIVER_SOURCE = _BROWSER_DRIVER_SOURCE_TEMPLATE.replace(
    "BOXKITE_READY_SENTINEL", _BROWSER_READY_SENTINEL
)


class _BrowserHandle:
    """Wraps the persistent browser-driver subprocess and its bookkeeping.

    Mirrors _NodeInterpreterHandle -- see its docstring for why this isn't
    a dataclass.
    """

    def __init__(self, proc: "asyncio.subprocess.Process", script_path: str):
        self.proc = proc
        self.script_path = script_path
        self.started_at = datetime.now().isoformat()
        self.last_used_at = _time.monotonic()


def _get_browser_lock() -> asyncio.Lock:
    """Lazily create the browser lock in the active event loop -- a separate
    lock from the Python/Node interpreters' own, so a call against one
    kept-alive process never blocks on another's spawn/exec/reset work."""
    if main._browser_lock is None:
        main._browser_lock = asyncio.Lock()
    return main._browser_lock


async def _kill_browser_handle(handle: "_BrowserHandle") -> None:
    """Terminate the browser-driver subprocess and remove its driver script.

    Killing the OS process is the ONLY teardown path -- there is no in-band
    "close" protocol action (see this module's docstring): SIGKILLing the
    driver process takes the real Chromium child process down with it
    (Playwright's own `browser.close()` never runs, but an unreachable
    orphaned Chromium child is still cleaned up by the OS once its parent,
    the driver, is gone and the process group is reaped the same way
    sidecar_processes.py's background processes are)."""
    proc = handle.proc
    if proc.returncode is None:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=10)
        except asyncio.TimeoutError:
            logger.warning("[browser] Driver process did not exit after kill()")
    try:
        os.remove(handle.script_path)
    except OSError:
        pass


async def _spawn_browser() -> "_BrowserHandle":
    """Start a fresh browser-driver subprocess.

    SECURITY (docs/BROWSER-EXEC-DESIGN.md §3.1): this is the ONLY
    sidecar-launched subprocess that opts out of the per-exec empty network
    namespace (`skip_network_isolation=True`) -- a real Chromium process
    must resolve DNS and open outbound HTTPS connections itself, to hosts
    nobody enumerated in advance. This does NOT widen the pod's own
    NetworkPolicy egress/ingress posture; it only lets THIS one process
    share the pod's own existing network namespace instead of getting a
    fresh, empty one. See src/boxkite/browser_network_policy.py for the
    NetworkPolicy this then requires. Applies identically in both runtime
    modes now (see get_sandbox_pid's docstring for why compose mode reuses
    the same nsenter path as K8s).
    """
    os.makedirs(main.TMP_DIR, exist_ok=True)
    script_path = os.path.join(main.TMP_DIR, f".boxkite-browser-{uuid4().hex}.js")
    with open(script_path, "w") as f:
        f.write(_BROWSER_DRIVER_SOURCE)
    os.chmod(script_path, 0o644)

    shell_command = f"exec node {script_path}"

    sandbox_pid = main.get_sandbox_pid()
    if not sandbox_pid:
        try:
            os.remove(script_path)
        except OSError:
            pass
        raise RuntimeError("Failed to find sandbox process")
    cmd = main.build_k8s_exec_command(sandbox_pid, shell_command, skip_network_isolation=True)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(main.SAFE_EXEC_ENV),
    )
    handle = _BrowserHandle(proc, script_path)

    try:
        ready_line = await asyncio.wait_for(
            proc.stdout.readline(), timeout=main.BROWSER_STARTUP_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        await _kill_browser_handle(handle)
        raise RuntimeError("Browser did not start in time")

    if _BROWSER_READY_SENTINEL not in ready_line.decode("utf-8", errors="replace"):
        await _kill_browser_handle(handle)
        raise RuntimeError("Browser driver failed to start")

    return handle


async def _get_or_spawn_browser_locked() -> "_BrowserHandle":
    """Return the live browser handle, respawning if absent, dead, or
    idle-expired. Caller MUST already hold _get_browser_lock() -- mirrors
    _get_or_spawn_node_interpreter_locked's own locking discipline."""
    handle = main._browser_handle
    still_fresh = (
        handle is not None
        and handle.proc.returncode is None
        and (_time.monotonic() - handle.last_used_at) <= main.BROWSER_IDLE_TIMEOUT_SECONDS
    )
    if still_fresh:
        return handle

    if handle is not None:
        await _kill_browser_handle(handle)
        main._browser_handle = None

    new_handle = await _spawn_browser()
    main._browser_handle = new_handle
    return new_handle


async def _reset_browser_locked() -> None:
    """Kill the current browser process, if any. Caller MUST hold the lock."""
    handle = main._browser_handle
    main._browser_handle = None
    if handle is not None:
        await _kill_browser_handle(handle)


async def _reset_browser() -> None:
    """Kill the current browser process (if any) so the next call starts
    fresh -- used by /browser/close, by /configure's session-wipe path (a
    recycled pod must never hand a new tenant a still-live, still-logged-in
    browser page from the previous tenant -- same cross-tenant leak
    _reset_node_interpreter's docstring describes), and by graceful
    shutdown. Called from /configure UNCONDITIONALLY, regardless of the
    current value of BOXKITE_BROWSER_ENABLED -- a browser process started
    while the flag was on must still be killed if the flag was since
    flipped off before this recycle (docs/BROWSER-EXEC-DESIGN.md §4)."""
    async with _get_browser_lock():
        await _reset_browser_locked()


async def _reap_idle_browser() -> None:
    """Kill the persistent browser if it has been idle past its timeout.

    Mirrors _reap_idle_node_interpreter -- called from the same periodic
    sync loop cadence."""
    async with _get_browser_lock():
        handle = main._browser_handle
        if handle is None:
            return
        if (_time.monotonic() - handle.last_used_at) <= main.BROWSER_IDLE_TIMEOUT_SECONDS:
            return
        main._browser_handle = None

    logger.info(
        f"[browser] Idle for over {main.BROWSER_IDLE_TIMEOUT_SECONDS}s; killing browser"
    )
    await _kill_browser_handle(handle)


async def _browser_dispatch_now(handle: "_BrowserHandle", request: dict, timeout: float) -> dict:
    """Send one request dict to a live browser handle and return its parsed
    {"data": ..., "error": ...} reply.

    Mirrors _node_interpreter_exec_now's transport shape (including reusing
    _read_interpreter_response_line, a helper generic over any
    stdin/stdout-JSON-lines subprocess, not Python/Node-specific). A
    timeout or transport-level failure here kills the whole browser process
    -- the next call starts a fresh one, the same contract the Python/Node
    interpreters already have. An application-level error (e.g. `page.goto`
    failing to resolve a host, or a thrown exception inside `browser_exec`'s
    script) is NOT a transport failure -- it comes back as a normal parsed
    response with `error` set, exactly like the interpreters' own
    script-level errors, and does not kill the process.
    """
    payload = json.dumps(request) + "\n"
    handle.proc.stdin.write(payload.encode("utf-8"))
    await handle.proc.stdin.drain()

    # Screenshots are the only response that can be large (a base64 PNG) --
    # bound the read the same way NODE_INTERPRETER's own cap does, off
    # BROWSER_MAX_SCREENSHOT_BYTES rather than a text-output cap.
    line_read_cap = main.BROWSER_MAX_SCREENSHOT_BYTES * 2 + _INTERPRETER_READ_CHUNK_BYTES
    try:
        raw_line = await asyncio.wait_for(
            _read_interpreter_response_line(handle.proc.stdout, line_read_cap),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        await _kill_browser_handle(handle)
        main._browser_handle = None
        raise TimeoutError(f"Browser call timed out after {timeout}s")

    if not raw_line:
        await _kill_browser_handle(handle)
        main._browser_handle = None
        raise RuntimeError("Browser driver process exited unexpectedly")

    try:
        parsed = json.loads(raw_line.decode("utf-8", errors="replace"))
    except Exception as exc:
        await _kill_browser_handle(handle)
        main._browser_handle = None
        raise RuntimeError(f"Malformed browser driver response: {exc}")

    handle.last_used_at = _time.monotonic()
    return parsed


@router.post("/browser/navigate", response_model=main.BrowserNavigateResponse)
async def browser_navigate(req: "main.BrowserNavigateRequest"):
    """
    Load `req.url` in the session's one browser page. Lazily starts the
    browser process on first call. See docs/BROWSER-EXEC-DESIGN.md §2.

    404s unless BOXKITE_BROWSER_ENABLED is set -- new attack surface, off
    by default, same posture as /node-interpreter/exec.
    """
    if not main.BOXKITE_BROWSER_ENABLED:
        raise HTTPException(status_code=404, detail="Browser tool is not enabled on this deployment.")

    if not req.url or not req.url.strip():
        raise HTTPException(status_code=400, detail="url is required")

    if req.wait_until not in main._BROWSER_ALLOWED_WAIT_UNTIL:
        raise HTTPException(
            status_code=400,
            detail=f"wait_until must be one of {sorted(main._BROWSER_ALLOWED_WAIT_UNTIL)}",
        )

    await main._reserve_session_exec_slot_or_raise(source="browser_navigate")

    timeout = min(max(1, req.timeout_seconds), main.BROWSER_MAX_EXEC_TIMEOUT_SECONDS)
    logger.info(f"[browser] navigate: {req.url[:200]}")

    _t0 = _time.monotonic()
    error_to_raise: Optional[HTTPException] = None
    response: Optional[dict] = None
    async with _get_browser_lock():
        try:
            handle = await _get_or_spawn_browser_locked()
            response = await _browser_dispatch_now(
                handle,
                {
                    "action": "navigate",
                    "url": req.url,
                    "wait_until": req.wait_until,
                    "timeout_ms": int(timeout * 1000),
                },
                timeout,
            )
        except (TimeoutError, RuntimeError) as exc:
            error_to_raise = HTTPException(status_code=502, detail=str(exc))

    duration_seconds = _time.monotonic() - _t0
    await main._record_session_exec_duration_or_raise(duration_seconds, source="browser_navigate")

    if error_to_raise is not None:
        raise error_to_raise

    data = response.get("data") or {}
    return main.BrowserNavigateResponse(
        title=data.get("title"),
        url=data.get("url"),
        status=data.get("status"),
        error=response.get("error"),
    )


@router.post("/browser/exec", response_model=main.BrowserExecResponse)
async def browser_exec(req: "main.BrowserExecRequest"):
    """
    Evaluate `req.script` in the current page's JS context (Playwright's
    `page.evaluate`, i.e. CDP `Runtime.evaluate`). Lazily starts the browser
    (with a blank page) if it isn't already running. See
    docs/BROWSER-EXEC-DESIGN.md §2.

    404s unless BOXKITE_BROWSER_ENABLED is set, same as /browser/navigate.
    """
    if not main.BOXKITE_BROWSER_ENABLED:
        raise HTTPException(status_code=404, detail="Browser tool is not enabled on this deployment.")

    if not req.script or not req.script.strip():
        raise HTTPException(status_code=400, detail="script is required")

    await main._reserve_session_exec_slot_or_raise(source="browser_exec")

    timeout = min(max(1, req.timeout_seconds), main.BROWSER_MAX_EXEC_TIMEOUT_SECONDS)
    logger.info(f"[browser] exec: {req.script[:200]}")

    _t0 = _time.monotonic()
    error_to_raise: Optional[HTTPException] = None
    response: Optional[dict] = None
    async with _get_browser_lock():
        try:
            handle = await _get_or_spawn_browser_locked()
            response = await _browser_dispatch_now(
                handle,
                {"action": "exec", "script": req.script},
                timeout,
            )
        except (TimeoutError, RuntimeError) as exc:
            error_to_raise = HTTPException(status_code=502, detail=str(exc))

    duration_seconds = _time.monotonic() - _t0
    await main._record_session_exec_duration_or_raise(duration_seconds, source="browser_exec")

    if error_to_raise is not None:
        raise error_to_raise

    data = response.get("data") or {}
    return main.BrowserExecResponse(result=data.get("result"), error=response.get("error"))


@router.post("/browser/screenshot", response_model=main.BrowserScreenshotResponse)
async def browser_screenshot(req: "main.BrowserScreenshotRequest"):
    """
    Return a base64 PNG of the current page (Playwright's `page.screenshot()`
    / CDP `Page.captureScreenshot`). Lazily starts the browser (with a blank
    page) if it isn't already running. See docs/BROWSER-EXEC-DESIGN.md §2.

    404s unless BOXKITE_BROWSER_ENABLED is set, same as /browser/navigate.
    Does not consume the session exec budget -- capturing a screenshot of
    an already-loaded page runs no new script/navigation.
    """
    if not main.BOXKITE_BROWSER_ENABLED:
        raise HTTPException(status_code=404, detail="Browser tool is not enabled on this deployment.")

    error_to_raise: Optional[HTTPException] = None
    response: Optional[dict] = None
    async with _get_browser_lock():
        try:
            handle = await _get_or_spawn_browser_locked()
            response = await _browser_dispatch_now(
                handle,
                {
                    "action": "screenshot",
                    "full_page": req.full_page,
                    "max_bytes": main.BROWSER_MAX_SCREENSHOT_BYTES,
                },
                main.BROWSER_MAX_EXEC_TIMEOUT_SECONDS,
            )
        except (TimeoutError, RuntimeError) as exc:
            error_to_raise = HTTPException(status_code=502, detail=str(exc))

    if error_to_raise is not None:
        raise error_to_raise

    if response.get("error"):
        return main.BrowserScreenshotResponse(image_base64=None, error=response["error"])

    data = response.get("data") or {}
    image_base64 = data.get("image_base64")
    # Defensive re-check server-side, even though the driver itself already
    # enforces max_bytes before ever base64-encoding the buffer -- never
    # trust a subprocess's own self-reported compliance as the only check.
    if image_base64:
        try:
            raw_len = len(base64.b64decode(image_base64, validate=False))
        except Exception:
            raw_len = 0
        if raw_len > main.BROWSER_MAX_SCREENSHOT_BYTES:
            return main.BrowserScreenshotResponse(
                image_base64=None,
                error=(
                    f"Screenshot is {raw_len} bytes, exceeding the "
                    f"{main.BROWSER_MAX_SCREENSHOT_BYTES}-byte cap; try full_page=false "
                    "or a smaller viewport"
                ),
            )
    return main.BrowserScreenshotResponse(image_base64=image_base64, error=None)


@router.post("/browser/close", response_model=main.BrowserCloseResponse)
async def browser_close():
    """Tear down the browser process (idempotent -- a no-op if none is
    running). The next /browser/navigate call starts a fresh one. See
    docs/BROWSER-EXEC-DESIGN.md §2.

    404s unless BOXKITE_BROWSER_ENABLED is set, same as /browser/navigate.
    """
    if not main.BOXKITE_BROWSER_ENABLED:
        raise HTTPException(status_code=404, detail="Browser tool is not enabled on this deployment.")
    await _reset_browser()
    return main.BrowserCloseResponse(status="closed")
