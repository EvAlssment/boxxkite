"""Agent-invokable language server completions (/lsp/*) -- issue #183.

See docs/LSP-SUPPORT-SCOPING.md for the full scoping writeup (what this
closes from issue #81, what remains explicitly deferred). Summary: opt-in,
off by default (BOXKITE_LSP_ENABLED), two language servers (pyright for
Python, typescript-language-server for TypeScript/JavaScript),
`textDocument/completion` only, full-document sync only (every open/edit
resends the whole file -- no incremental didChange deltas).

Transport model, deliberately NOT a raw WS bridge mirroring `WS /pty`: an
LLM tool-calling loop needs request/response, exactly like every other
tool here, not a raw byte stream (`WS /pty` exists specifically because a
*human* is on the other end of that socket -- see SECURITY.md's "Human
takeover" section). This module follows `sidecar_node_interpreter.py`'s
precedent instead: one kept-alive subprocess per LSP handle, with the
sidecar owning the wire protocol internally. The one genuine difference
from the interpreters is LSP's own framing (`Content-Length`-prefixed
JSON-RPC, not newline-delimited JSON) and its request/response
correlation by numeric id (a real language server also emits unsolicited
notifications, e.g. `textDocument/publishDiagnostics` -- read and
discarded here, not queued or surfaced; see the module docstring's
"Deferred" list in the scoping doc for why).

Follows the same ``main.<NAME>`` state-ownership convention as every
sibling ``sidecar_*.py`` module (GitHub issue #71's refactor): all
config/models/registry state live on ``main`` and are read/written via
``main.`` at call time so tests that monkeypatch attributes on ``main``
still take effect, and ``get_sandbox_pid``/``build_k8s_exec_command``/
``_signal_process_group`` are called via ``main.`` for the same reason.
"""

import asyncio
import json
import logging
import shlex
import signal
import time as _time
from datetime import datetime
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException

import main

logger = logging.getLogger("sidecar")

router = APIRouter()


# No caller-supplied argv, ever: the actual binary invoked is fixed by this
# server-side map, keyed only by a `language` enum string validated against
# this same dict below -- never user input reaching exec() (the same
# reason sidecar_node_interpreter.py needs no command-whitelist check: it
# doesn't take an arbitrary command either).
_LSP_SERVER_COMMANDS: dict[str, list[str]] = {
    "python": ["pyright-langserver", "--stdio"],
    "typescript": ["typescript-language-server", "--stdio"],
}

_LANGUAGE_IDS: dict[str, str] = {
    "python": "python",
    "typescript": "typescript",
}


class LspServerHandle:
    """Tracks one persistent language-server subprocess and its JSON-RPC
    request/response bookkeeping.

    Mirrors _NodeInterpreterHandle/ProcessHandle's shape -- not a
    dataclass, since `pending`/`next_id`/`open_documents`/`last_used_at`
    are all mutated in place across the handle's lifetime.
    """

    def __init__(self, lsp_id: str, language: str, proc: "asyncio.subprocess.Process"):
        self.lsp_id = lsp_id
        self.language = language
        self.proc = proc
        self.started_at = datetime.now().isoformat()
        self.last_used_at = _time.monotonic()
        self.next_id = 0
        self.pending: dict[int, "asyncio.Future"] = {}
        self.initialized = False
        self.open_documents: set[str] = set()
        self.document_versions: dict[str, int] = {}
        self.reader_task: Optional["asyncio.Task"] = None
        self.stderr_task: Optional["asyncio.Task"] = None


def _get_lsp_registry_lock() -> asyncio.Lock:
    """Lazily create the LSP registry lock in the active event loop (same
    pattern as _get_process_registry_lock/_get_node_interpreter_lock)."""
    if main._lsp_registry_lock is None:
        main._lsp_registry_lock = asyncio.Lock()
    return main._lsp_registry_lock


def _get_lsp_handle_or_404(lsp_id: str) -> "LspServerHandle":
    handle = main._lsp_registry.get(lsp_id)
    if handle is None:
        raise HTTPException(status_code=404, detail=f"LSP server {lsp_id} not found")
    return handle


# ============================================================================
# Content-Length-framed JSON-RPC framing. The one genuinely new wire-format
# piece in this codebase (every other kept-alive process here speaks plain
# newline-delimited JSON) -- kept in small, pure, independently-testable
# functions for exactly that reason.
# ============================================================================


def _frame_message(payload: dict) -> bytes:
    """Encode one JSON-RPC message as `Content-Length: N\\r\\n\\r\\n<json>`.

    N is a BYTE length, not a character count -- a body containing
    multi-byte UTF-8 characters would otherwise desync the next frame's
    header from its body. `ensure_ascii=False` (matching how a real
    language server's own JSON serializer -- e.g. Node's JSON.stringify --
    emits raw UTF-8 rather than escaping every non-ASCII code point) is
    what actually makes this distinction real: source code containing
    non-Latin identifiers or comments is exactly the payload this framing
    has to get right.
    """
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


async def _read_one_frame(stream: "asyncio.StreamReader") -> Optional[dict]:
    """Read and decode one Content-Length-framed JSON-RPC message.

    Returns None on a clean EOF (the server process exited or closed its
    stdout) rather than raising, so callers can treat "no more frames" as
    an ordinary loop-termination condition instead of an exception path.
    """
    headers: dict[bytes, bytes] = {}
    while True:
        line = await stream.readline()
        if not line:
            return None
        line = line.rstrip(b"\r\n")
        if line == b"":
            break
        if b":" not in line:
            continue
        key, _, value = line.partition(b":")
        headers[key.strip().lower()] = value.strip()

    try:
        content_length = int(headers.get(b"content-length", b"0"))
    except ValueError:
        return None
    if content_length <= 0:
        return {}

    try:
        body = await stream.readexactly(content_length)
    except asyncio.IncompleteReadError:
        return None
    return json.loads(body.decode("utf-8"))


async def _lsp_reader_loop(handle: "LspServerHandle") -> None:
    """Continuously read frames from the server's stdout, resolving pending
    request futures by JSON-RPC id, discarding anything else.

    A frame with no `id` (or an id this handle never sent, e.g. a stray
    late response) is a server-initiated notification --
    `textDocument/publishDiagnostics`, `window/logMessage`,
    `$/typescriptVersion`, etc. These are logged at debug and discarded,
    never queued or surfaced to the tool caller -- the explicit, named
    deferral of #81 point (2)'s "unsolicited notifications" half (see
    docs/LSP-SUPPORT-SCOPING.md), not silent data loss: nothing this
    module promises to deliver is dropped, since this feature never
    promised push notifications in the first place.
    """
    try:
        while True:
            frame = await _read_one_frame(handle.proc.stdout)
            if frame is None:
                break
            msg_id = frame.get("id")
            future = handle.pending.pop(msg_id, None) if msg_id is not None else None
            if future is not None:
                if not future.done():
                    future.set_result(frame)
            else:
                logger.debug(
                    f"[lsp:{handle.lsp_id}] discarding notification: {frame.get('method')}"
                )
    except Exception as e:
        logger.error(f"[lsp:{handle.lsp_id}] reader loop error: {e}")
    finally:
        for future in list(handle.pending.values()):
            if not future.done():
                future.set_exception(RuntimeError("LSP server process exited unexpectedly"))
        handle.pending.clear()


async def _drain_stderr(handle: "LspServerHandle") -> None:
    """Continuously drain stderr so the pipe never backpressures the
    server process into blocking -- discarded, not surfaced (mirrors
    _process_reader_loop's "never let an unread pipe stall the process"
    reasoning, applied to a stream this feature has no use for)."""
    try:
        stream = handle.proc.stderr
        while stream is not None:
            chunk = await stream.read(4096)
            if not chunk:
                break
    except Exception:
        pass


async def _send_request(
    handle: "LspServerHandle", method: str, params: Optional[dict], timeout: float
) -> Optional[dict]:
    """Send one JSON-RPC request and await its correlated response.

    Raises TimeoutError if no response arrives in time (the pending
    future is discarded either way -- a late response for an abandoned id
    is harmless since _lsp_reader_loop no-ops on an unknown id), and
    RuntimeError if the server itself returned a JSON-RPC error object.
    """
    handle.next_id += 1
    request_id = handle.next_id
    message = {"jsonrpc": "2.0", "id": request_id, "method": method, "params": params}

    loop = asyncio.get_event_loop()
    future: "asyncio.Future" = loop.create_future()
    handle.pending[request_id] = future

    handle.proc.stdin.write(_frame_message(message))
    await handle.proc.stdin.drain()

    try:
        frame = await asyncio.wait_for(future, timeout=timeout)
    except asyncio.TimeoutError:
        handle.pending.pop(request_id, None)
        raise TimeoutError(f"LSP request {method!r} timed out after {timeout}s")

    if "error" in frame:
        raise RuntimeError(f"LSP server error for {method!r}: {frame['error']}")
    return frame.get("result")


async def _send_notification(handle: "LspServerHandle", method: str, params: Optional[dict]) -> None:
    """Send a JSON-RPC notification (no id, no response expected) --
    `initialized`, `textDocument/didOpen`, `textDocument/didChange`,
    `exit` are all notifications per the LSP spec."""
    message = {"jsonrpc": "2.0", "method": method, "params": params}
    handle.proc.stdin.write(_frame_message(message))
    await handle.proc.stdin.drain()


# ============================================================================
# Process lifecycle
# ============================================================================


async def _spawn_lsp_server(language: str, lsp_id: str) -> "LspServerHandle":
    """Start a fresh language-server subprocess and run its initialize
    handshake.

    Mirrors _spawn_node_interpreter's exact namespace-entry mechanism (same
    nsenter/unshare flags, same UID drop, same SAFE_EXEC_ENV, in both
    runtime modes) -- the difference is the spawned process
    speaks real LSP JSON-RPC over stdio rather than a small custom
    newline-JSON driver protocol, so there is no driver script to write to
    disk first; `_LSP_SERVER_COMMANDS[language]` execs the real language
    server binary directly. `start_new_session=True` (matching
    sidecar_processes.py's ProcessHandle, not the interpreters) since
    teardown uses `_signal_process_group` the same way background
    processes do -- nsenter's own internal fork means the tracked
    `asyncio.subprocess.Process` pid is one level above the real language
    server binary in K8s mode; see `_signal_process_group`'s own docstring.
    """
    argv = _LSP_SERVER_COMMANDS[language]
    shell_command = "exec " + shlex.join(argv)

    sandbox_pid = main.get_sandbox_pid()
    if not sandbox_pid:
        raise RuntimeError("Failed to find sandbox process")
    cmd = main.build_k8s_exec_command(sandbox_pid, shell_command)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(main.SAFE_EXEC_ENV),
        start_new_session=True,
    )
    handle = LspServerHandle(lsp_id=lsp_id, language=language, proc=proc)
    handle.reader_task = asyncio.create_task(_lsp_reader_loop(handle))
    handle.stderr_task = asyncio.create_task(_drain_stderr(handle))

    root_uri = f"file://{main.WORKSPACE_DIR}"
    try:
        await _send_request(
            handle,
            "initialize",
            {
                "processId": None,
                "rootUri": root_uri,
                "capabilities": {
                    "textDocument": {
                        "completion": {
                            "completionItem": {"snippetSupport": False},
                        },
                        "synchronization": {"didSave": False, "willSave": False},
                    },
                },
            },
            timeout=main.LSP_STARTUP_TIMEOUT_SECONDS,
        )
    except (TimeoutError, RuntimeError):
        await _kill_lsp_handle(handle)
        raise

    await _send_notification(handle, "initialized", {})
    handle.initialized = True
    return handle


async def _kill_lsp_handle(handle: "LspServerHandle") -> None:
    """Hard-kill the language server's whole process group and cancel its
    background tasks. Idempotent -- safe to call on an already-exited
    process (e.g. as the final step of a graceful _stop_lsp_handle)."""
    for task in (handle.reader_task, handle.stderr_task):
        if task is not None:
            task.cancel()

    if handle.proc.returncode is None:
        main._signal_process_group(handle.proc, signal.SIGKILL)
        try:
            await asyncio.wait_for(handle.proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning(f"[lsp:{handle.lsp_id}] process did not exit after kill()")

    for future in list(handle.pending.values()):
        if not future.done():
            future.set_exception(RuntimeError("LSP server killed"))
    handle.pending.clear()


async def _stop_lsp_handle(handle: "LspServerHandle") -> None:
    """Graceful `shutdown`/`exit` per the LSP spec, falling back to a hard
    process-group kill if the server doesn't exit within the grace period
    (mirrors _stop_process's SIGTERM-then-SIGKILL shape, adapted to LSP's
    own request-based shutdown handshake instead of a POSIX signal)."""
    if handle.proc.returncode is None:
        try:
            await _send_request(handle, "shutdown", None, timeout=main.LSP_SHUTDOWN_TIMEOUT_SECONDS)
            await _send_notification(handle, "exit", None)
        except Exception as e:
            logger.warning(f"[lsp:{handle.lsp_id}] graceful shutdown handshake failed: {e}")

        try:
            await asyncio.wait_for(handle.proc.wait(), timeout=main.LSP_SHUTDOWN_GRACE_PERIOD_SECONDS)
        except asyncio.TimeoutError:
            main._signal_process_group(handle.proc, signal.SIGKILL)

    await _kill_lsp_handle(handle)


async def _kill_all_lsp_servers() -> int:
    """SIGKILL every tracked LSP server and clear the registry.

    Mandatory before any pod-identity change (a recycled pod claimed by a
    different tenant) and before graceful shutdown -- a leaked language
    server process across a pod recycle is the same cross-tenant leak
    class SECURITY.md already documents for background processes/pty/
    interpreters (its open documents could contain a previous tenant's
    source code). Called from /configure and shutdown_event, same as
    _kill_all_processes/_reset_node_interpreter/_reset_browser.
    """
    lock = _get_lsp_registry_lock()
    async with lock:
        handles = list(main._lsp_registry.values())
        main._lsp_registry.clear()

    killed = 0
    for handle in handles:
        if handle.proc.returncode is None:
            killed += 1
        await _kill_lsp_handle(handle)
    return killed


async def _reap_idle_lsp_servers() -> None:
    """Kill any LSP server idle past LSP_IDLE_TIMEOUT_SECONDS. Called from
    the periodic sync loop, same cadence as _reap_idle_interpreter/
    _reap_idle_node_interpreter/_reap_idle_browser."""
    lock = _get_lsp_registry_lock()
    async with lock:
        idle_ids = [
            lsp_id
            for lsp_id, handle in main._lsp_registry.items()
            if (_time.monotonic() - handle.last_used_at) > main.LSP_IDLE_TIMEOUT_SECONDS
        ]
        idle_handles = [main._lsp_registry.pop(lsp_id) for lsp_id in idle_ids]

    for handle in idle_handles:
        logger.info(f"[lsp:{handle.lsp_id}] idle past {main.LSP_IDLE_TIMEOUT_SECONDS}s; killing")
        await _kill_lsp_handle(handle)


# ============================================================================
# Document sync (full-document only -- see module docstring) and completion
# ============================================================================


def _uri_for_path(path: str) -> str:
    """Best-effort file:// URI for a workspace-relative or absolute path.

    Not a full virtual-path resolver (sidecar_paths.py's own machinery) --
    LSP's document URI just needs to be a stable, unique identifier the
    server and this module agree on across open/didChange/completion calls
    for the SAME file; it does not need to satisfy sidecar_paths.py's
    multi-root (workspace/outputs/uploads/skills) rules.
    """
    absolute_path = path if path.startswith("/") else f"{main.WORKSPACE_DIR}/{path}"
    return f"file://{absolute_path}"


async def _lsp_open_document(handle: "LspServerHandle", path: str, content: str) -> None:
    """`textDocument/didOpen` on first use, `textDocument/didChange`
    (full-document replacement, no incremental range) on every call after
    -- the explicit, deliberate full-document-sync answer to #81 point (3):
    every call resends the whole current file content rather than
    maintaining an incremental virtual-buffer diff, which is fine for
    agent-paced tool calls and explicitly not meant for real editor
    keystroke-by-keystroke usage (see docs/LSP-SUPPORT-SCOPING.md).
    """
    uri = _uri_for_path(path)
    if uri in handle.open_documents:
        version = handle.document_versions.get(uri, 1) + 1
        handle.document_versions[uri] = version
        await _send_notification(
            handle,
            "textDocument/didChange",
            {
                "textDocument": {"uri": uri, "version": version},
                "contentChanges": [{"text": content}],
            },
        )
    else:
        handle.open_documents.add(uri)
        handle.document_versions[uri] = 1
        await _send_notification(
            handle,
            "textDocument/didOpen",
            {
                "textDocument": {
                    "uri": uri,
                    "languageId": _LANGUAGE_IDS[handle.language],
                    "version": 1,
                    "text": content,
                },
            },
        )
    handle.last_used_at = _time.monotonic()


async def _lsp_completion(
    handle: "LspServerHandle", path: str, line: int, character: int, timeout: float
) -> list:
    """`textDocument/completion` at a position, normalized to a plain list
    -- the LSP spec permits the result to be `null`, a bare
    `CompletionItem[]`, or a `CompletionList {isIncomplete, items}`; every
    shape is handled explicitly rather than assuming one."""
    uri = _uri_for_path(path)
    result = await _send_request(
        handle,
        "textDocument/completion",
        {
            "textDocument": {"uri": uri},
            "position": {"line": line, "character": character},
        },
        timeout=timeout,
    )
    handle.last_used_at = _time.monotonic()
    if result is None:
        return []
    if isinstance(result, dict):
        return result.get("items", [])
    if isinstance(result, list):
        return result
    return []


# ============================================================================
# Routes
# ============================================================================


@router.post("/lsp/start", response_model=main.LspStartResponse, status_code=201)
async def lsp_start(req: "main.LspStartRequest"):
    """Start a persistent language server for one session and run its
    initialize handshake. 404s unless BOXKITE_LSP_ENABLED is set -- new
    attack surface, off by default, same posture as /pty-exec and
    /node-interpreter/exec.

    Session exec budget (GitHub issue #122): shares the exact same
    counters/sticky flag as /exec, /interpreter/exec, /node-interpreter/exec,
    and /process/start -- reserved (and count-checked) before the spawn+
    handshake runs, duration recorded (and seconds-checked) after it
    finishes, regardless of success. A new exec-capable route that skips
    this reintroduces the exact CRITICAL bug class SECURITY.md's "Known
    follow-ups" section documents for the original /exec-only pass.
    """
    if not main.BOXKITE_LSP_ENABLED:
        raise HTTPException(status_code=404, detail="LSP support is not enabled on this deployment.")
    if req.language not in _LSP_SERVER_COMMANDS:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported language {req.language!r}; supported: {sorted(_LSP_SERVER_COMMANDS)}",
        )

    await main._reserve_session_exec_slot_or_raise(source="lsp_start")

    lock = _get_lsp_registry_lock()
    async with lock:
        active = len(main._lsp_registry)
        if active >= main.LSP_MAX_SERVERS:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Session already has {active} LSP server(s) running "
                    f"(max {main.LSP_MAX_SERVERS}). Stop one before starting another."
                ),
            )

    lsp_id = f"lsp_{uuid4().hex}"
    _t0 = _time.monotonic()
    error_to_raise: Optional[HTTPException] = None
    handle: Optional["LspServerHandle"] = None
    try:
        handle = await _spawn_lsp_server(req.language, lsp_id)
    except Exception as exc:
        logger.error(f"[lsp:start] failed to start {req.language}: {exc}")
        error_to_raise = HTTPException(status_code=502, detail=f"Failed to start LSP server: {exc}")

    # Register the handle BEFORE recording duration, not after -- a
    # successfully spawned process must already be visible to
    # _kill_all_lsp_servers() (called by _teardown_session_for_budget_breach
    # below) the moment this exact call is the one that crosses the
    # exec-seconds ceiling. Registering only on the "happy path" after
    # duration-recording would leak that real subprocess: still running,
    # untracked, and immune to the very teardown its own breach triggers.
    if handle is not None:
        async with lock:
            main._lsp_registry[lsp_id] = handle

    duration_seconds = _time.monotonic() - _t0
    await main._record_session_exec_duration_or_raise(duration_seconds, source="lsp_start")

    if error_to_raise is not None:
        raise error_to_raise

    logger.info(f"[lsp:start] {lsp_id}: language={req.language}")
    return main.LspStartResponse(lsp_id=lsp_id)


@router.post("/lsp/{lsp_id}/open", response_model=main.LspOpenResponse)
async def lsp_open(lsp_id: str, req: "main.LspOpenRequest"):
    """Open (or, on a later call for the same path, full-document-replace)
    a document on a running language server. Not budget-checked -- this is
    a document-sync notification with no RPC response awaited, the same
    "not exec-like" classification /process/input and /interpreter/reset
    already have."""
    if not main.BOXKITE_LSP_ENABLED:
        raise HTTPException(status_code=404, detail="LSP support is not enabled on this deployment.")
    handle = _get_lsp_handle_or_404(lsp_id)
    try:
        await _lsp_open_document(handle, req.path, req.content)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"Failed to open document on LSP server: {exc}")
    return main.LspOpenResponse(status="opened")


@router.post("/lsp/{lsp_id}/completion", response_model=main.LspCompletionResponse)
async def lsp_completion(lsp_id: str, req: "main.LspCompletionRequest"):
    """Request completions at a position from a running language server.

    Session exec budget: same shared counters as /lsp/start (see its
    docstring) -- this is the actual "run code" moment for this feature
    (a real RPC round-trip to a real language-analysis process), so it
    gets the same treatment /exec/interpreter calls do, not the
    not-budget-checked treatment /lsp/open gets.
    """
    if not main.BOXKITE_LSP_ENABLED:
        raise HTTPException(status_code=404, detail="LSP support is not enabled on this deployment.")
    handle = _get_lsp_handle_or_404(lsp_id)

    await main._reserve_session_exec_slot_or_raise(source="lsp_completion")

    timeout = main.LSP_REQUEST_TIMEOUT_SECONDS
    _t0 = _time.monotonic()
    error_to_raise: Optional[HTTPException] = None
    items: list = []
    try:
        items = await _lsp_completion(handle, req.path, req.line, req.character, timeout)
    except (TimeoutError, RuntimeError) as exc:
        error_to_raise = HTTPException(status_code=502, detail=f"LSP completion request failed: {exc}")

    duration_seconds = _time.monotonic() - _t0
    await main._record_session_exec_duration_or_raise(duration_seconds, source="lsp_completion")

    if error_to_raise is not None:
        raise error_to_raise
    return main.LspCompletionResponse(items=items)


@router.post("/lsp/{lsp_id}/stop", response_model=main.LspStopResponse)
async def lsp_stop(lsp_id: str):
    """Gracefully shut down a running language server and remove it from
    the registry. Not budget-checked, same as /interpreter/reset and
    /process/stop."""
    if not main.BOXKITE_LSP_ENABLED:
        raise HTTPException(status_code=404, detail="LSP support is not enabled on this deployment.")

    lock = _get_lsp_registry_lock()
    async with lock:
        handle = main._lsp_registry.pop(lsp_id, None)
    if handle is None:
        raise HTTPException(status_code=404, detail=f"LSP server {lsp_id} not found")

    await _stop_lsp_handle(handle)
    return main.LspStopResponse(status="stopped")
