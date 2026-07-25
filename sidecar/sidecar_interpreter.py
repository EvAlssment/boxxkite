"""Persistent Python interpreter (one kept-alive process per session) and the
/interpreter/* routes.

Split out of the original monolithic ``main.py`` (GitHub issue #71) as a pure
mechanical refactor -- no behavior change. The ``_interpreter_handle`` /
``_interpreter_lock`` state and all config/models remain owned by ``main`` and
are referenced via ``main.<NAME>``; ``get_sandbox_pid``/``build_k8s_exec_command``
are called via ``main.`` so monkeypatching is observed.
"""

import asyncio
import json
import logging
import os
import time as _time
from datetime import datetime
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, HTTPException

import main

logger = logging.getLogger("sidecar")

router = APIRouter()


# Persistent Python interpreter (see the INTERPRETER_* constants in main)
# ============================================================================

# Written to stdout by the driver script the moment it's ready to accept
# requests, so _spawn_interpreter() doesn't race sending the first snippet
# before the interpreter has even started reading stdin.
_INTERPRETER_READY_SENTINEL = "__BOXKITE_INTERPRETER_READY__"

# Runs inside the sandbox (nsenter'd exactly like exec_in_sandbox does for
# one-shot commands), reading one JSON request per line from stdin
# and writing one JSON response per line to stdout. Kept deliberately small:
# no ipykernel/Jupyter protocol dependency, just enough of a REPL loop to give
# "stdout + repr of the last expression, against a namespace that persists
# across calls" -- the scope this feature actually asked for.
_INTERPRETER_DRIVER_SOURCE_TEMPLATE = """
import ast
import contextlib
import io
import json
import sys
import traceback

_globals = {"__name__": "__main__", "__builtins__": __builtins__}

sys.stdout.write("BOXKITE_READY_SENTINEL\\n")
sys.stdout.flush()

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        request = json.loads(line)
        code = request.get("code", "")
    except Exception as exc:
        sys.stdout.write(json.dumps({"stdout": "", "result": None, "error": f"Invalid request: {exc}"}) + "\\n")
        sys.stdout.flush()
        continue

    buf = io.StringIO()
    result_repr = None
    error_text = None
    try:
        tree = ast.parse(code, mode="exec")
        last_expr = None
        if tree.body and isinstance(tree.body[-1], ast.Expr):
            last_expr = tree.body.pop()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(buf):
            if tree.body:
                exec(compile(tree, "<interpreter>", "exec"), _globals)
            if last_expr is not None:
                expr = ast.fix_missing_locations(ast.Expression(body=last_expr.value))
                value = eval(compile(expr, "<interpreter>", "eval"), _globals)
                if value is not None:
                    result_repr = repr(value)
    except Exception:
        error_text = traceback.format_exc()

    sys.stdout.write(
        json.dumps({"stdout": buf.getvalue(), "result": result_repr, "error": error_text}) + "\\n"
    )
    sys.stdout.flush()
"""

# BOXKITE_READY_SENTINEL is a literal placeholder token (not a real Python
# name -- it's substituted below), kept distinct from the constant's own
# name so a reader can't confuse the driver-source template with live code.
_INTERPRETER_DRIVER_SOURCE = _INTERPRETER_DRIVER_SOURCE_TEMPLATE.replace(
    "BOXKITE_READY_SENTINEL", _INTERPRETER_READY_SENTINEL
)


class _InterpreterHandle:
    """Wraps the persistent interpreter subprocess and its bookkeeping.

    Not a dataclass: `proc` and `last_used_at` are both mutated in place
    (last_used_at on every successful call), which fits a plain class better
    than an immutable value type here.
    """

    def __init__(self, proc: "asyncio.subprocess.Process", script_path: str):
        self.proc = proc
        self.script_path = script_path
        self.started_at = datetime.now().isoformat()
        self.last_used_at = _time.monotonic()


def _get_interpreter_lock() -> asyncio.Lock:
    """Lazily create the interpreter lock in the active event loop.

    Serializes every operation that reads or replaces `_interpreter_handle`
    -- get-or-spawn, a single exec call, reset, idle-reap, and the
    /configure and shutdown teardown paths -- so a respawn can never race a
    concurrent user of the handle it's replacing.
    """
    if main._interpreter_lock is None:
        main._interpreter_lock = asyncio.Lock()
    return main._interpreter_lock


async def _kill_interpreter_handle(handle: "_InterpreterHandle") -> None:
    """Terminate the interpreter subprocess and remove its driver script."""
    proc = handle.proc
    if proc.returncode is None:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("[interpreter] Process did not exit after kill()")
    try:
        os.remove(handle.script_path)
    except OSError:
        pass


async def _spawn_interpreter() -> "_InterpreterHandle":
    """Start a fresh persistent interpreter process.

    Mirrors exec_in_sandbox's exact namespace-entry mechanism (same
    nsenter/unshare flags, same UID drop, same SAFE_EXEC_ENV, in both
    runtime modes) -- the only difference is the spawned process
    is a long-lived driver loop reading JSON off stdin instead of a one-shot
    `sh -c <command>`. The memory cap is enforced the same way a one-shot
    exec enforces its output cap: a resource limit on the process itself
    (`ulimit -v`), not a cooperative check the interpreter could evade.
    """
    os.makedirs(main.TMP_DIR, exist_ok=True)
    script_path = os.path.join(main.TMP_DIR, f".boxkite-interpreter-{uuid4().hex}.py")
    with open(script_path, "w") as f:
        f.write(_INTERPRETER_DRIVER_SOURCE)
    os.chmod(script_path, 0o644)

    mem_kb = main.INTERPRETER_MAX_MEMORY_MB * 1024
    shell_command = f"ulimit -v {mem_kb}; exec python3 -u {script_path}"

    sandbox_pid = main.get_sandbox_pid()
    if not sandbox_pid:
        try:
            os.remove(script_path)
        except OSError:
            pass
        raise RuntimeError("Failed to find sandbox process")
    cmd = main.build_k8s_exec_command(sandbox_pid, shell_command)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=dict(main.SAFE_EXEC_ENV),
    )
    handle = _InterpreterHandle(proc, script_path)

    try:
        ready_line = await asyncio.wait_for(
            proc.stdout.readline(), timeout=main.INTERPRETER_STARTUP_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        await _kill_interpreter_handle(handle)
        raise RuntimeError("Interpreter did not start in time")

    if _INTERPRETER_READY_SENTINEL not in ready_line.decode("utf-8", errors="replace"):
        await _kill_interpreter_handle(handle)
        raise RuntimeError("Interpreter failed to start")

    return handle


async def _get_or_spawn_interpreter_locked() -> "_InterpreterHandle":
    """Return the live interpreter, respawning if absent, dead, or idle-expired.

    Caller MUST already hold _get_interpreter_lock() -- this function does
    not acquire it, so a caller can hold the lock across both "get/spawn the
    handle" and "use the handle" without risking a concurrent reset/reap
    swapping the handle out from under it mid-call.
    """
    handle = main._interpreter_handle
    still_fresh = (
        handle is not None
        and handle.proc.returncode is None
        and (_time.monotonic() - handle.last_used_at) <= main.INTERPRETER_IDLE_TIMEOUT_SECONDS
    )
    if still_fresh:
        return handle

    if handle is not None:
        await _kill_interpreter_handle(handle)
        main._interpreter_handle = None

    new_handle = await _spawn_interpreter()
    main._interpreter_handle = new_handle
    return new_handle


async def _reset_interpreter_locked() -> None:
    """Kill the current interpreter, if any. Caller MUST hold the lock."""
    handle = main._interpreter_handle
    main._interpreter_handle = None
    if handle is not None:
        await _kill_interpreter_handle(handle)


async def _reset_interpreter() -> None:
    """Kill the current interpreter (if any) so the next call starts fresh.

    Used by /interpreter/reset, by /configure's session-wipe path -- so a
    recycled pod never hands a new tenant a still-live interpreter and its
    globals from a previous tenant, the same cross-tenant leak
    docs/PROCESS-SESSIONS-DESIGN.md's §2(b) flags for kept-alive background
    processes generally -- and by graceful shutdown.
    """
    async with _get_interpreter_lock():
        await _reset_interpreter_locked()


async def _reap_idle_interpreter() -> None:
    """Kill the persistent interpreter if it has been idle past its timeout.

    Called from the periodic sync loop (same cadence as the existing
    flush/reconcile sweep) so a forgotten interpreter doesn't hold memory
    for the rest of a long session just because nobody called
    /interpreter/reset -- the idle-timeout counterpart to
    INTERPRETER_MAX_MEMORY_MB's per-process memory cap.
    """
    async with _get_interpreter_lock():
        handle = main._interpreter_handle
        if handle is None:
            return
        if (_time.monotonic() - handle.last_used_at) <= main.INTERPRETER_IDLE_TIMEOUT_SECONDS:
            return
        main._interpreter_handle = None

    logger.info(
        f"[interpreter] Idle for over {main.INTERPRETER_IDLE_TIMEOUT_SECONDS}s; killing interpreter"
    )
    await _kill_interpreter_handle(handle)


# Chunk size for _read_interpreter_response_line's manual accumulation --
# arbitrary but matches asyncio's own default StreamReader chunk size.
_INTERPRETER_READ_CHUNK_BYTES = 65536


async def _read_interpreter_response_line(
    stream: "asyncio.StreamReader", max_bytes: int
) -> bytes:
    """Read one newline-terminated line without asyncio's default 64KB limit.

    `StreamReader.readline()`/`readuntil()` raise LimitOverrunError once a
    line exceeds the reader's configured `limit` (64KB by default) -- well
    below INTERPRETER_MAX_OUTPUT_BYTES's advertised 256KB default, so a
    single call with a large-but-supported amount of stdout/stderr would
    500 instead of being truncated. `read()` has no such per-line limit, so
    accumulate chunks via `read()` until a newline appears, capping total
    bytes read at `max_bytes` (with margin over INTERPRETER_MAX_OUTPUT_BYTES
    for JSON-encoding overhead) so a runaway process that never emits a
    newline still can't grow this buffer without bound.
    """
    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(_INTERPRETER_READ_CHUNK_BYTES)
        if not chunk:
            break
        chunks.append(chunk)
        total += len(chunk)
        if b"\n" in chunk or total >= max_bytes:
            break
    return b"".join(chunks)


async def _interpreter_exec_now(
    handle: "_InterpreterHandle", code: str, timeout: int
) -> "main.InterpreterExecResponse":
    """Send one code snippet to a live interpreter handle and read its reply.

    Caller MUST hold _get_interpreter_lock(). On any protocol/timeout
    failure the interpreter process is killed and the exception propagates
    -- the caller is responsible for clearing `_interpreter_handle` (done
    via _get_or_spawn_interpreter_locked() on the next call) so a broken
    process is never reused.
    """
    payload = json.dumps({"code": code}) + "\n"
    handle.proc.stdin.write(payload.encode("utf-8"))
    await handle.proc.stdin.drain()

    # Margin over INTERPRETER_MAX_OUTPUT_BYTES covers JSON-string escaping
    # overhead on the stdout field plus the result/error fields sharing the
    # same response line.
    line_read_cap = main.INTERPRETER_MAX_OUTPUT_BYTES * 4 + _INTERPRETER_READ_CHUNK_BYTES
    try:
        raw_line = await asyncio.wait_for(
            _read_interpreter_response_line(handle.proc.stdout, line_read_cap),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        await _kill_interpreter_handle(handle)
        main._interpreter_handle = None
        raise TimeoutError(f"Interpreter call timed out after {timeout}s")

    if not raw_line:
        await _kill_interpreter_handle(handle)
        main._interpreter_handle = None
        raise RuntimeError("Interpreter process exited unexpectedly")

    try:
        parsed = json.loads(raw_line.decode("utf-8", errors="replace"))
    except Exception as exc:
        await _kill_interpreter_handle(handle)
        main._interpreter_handle = None
        raise RuntimeError(f"Malformed interpreter response: {exc}")

    handle.last_used_at = _time.monotonic()

    stdout_text = str(parsed.get("stdout") or "")
    truncated = False
    stdout_bytes = stdout_text.encode("utf-8")
    if len(stdout_bytes) > main.INTERPRETER_MAX_OUTPUT_BYTES:
        stdout_text = stdout_bytes[:main.INTERPRETER_MAX_OUTPUT_BYTES].decode("utf-8", errors="ignore")
        truncated = True

    return main.InterpreterExecResponse(
        stdout=stdout_text,
        result=parsed.get("result"),
        error=parsed.get("error"),
        truncated=truncated,
    )


@router.post("/interpreter/exec", response_model=main.InterpreterExecResponse)
async def interpreter_exec(req: main.InterpreterExecRequest):
    """
    Execute a code snippet against a persistent, kept-alive Python
    interpreter for the current session.

    Unlike /exec (which always spawns a fresh `python3 -c ...` process),
    variables assigned in one call are visible to later calls, until the
    interpreter is reset (/interpreter/reset), times out from inactivity
    (INTERPRETER_IDLE_TIMEOUT_SECONDS), or the session is reconfigured or
    torn down.

    Session exec budget (GitHub issue #122): this route ships enabled by
    default, same as bash_tool -- a security review found the budget was
    originally wired into /exec only, so an agent looping via this route
    instead spent zero budget and was never throttled, and could keep
    calling this route completely unobstructed even after already
    tripping the sticky budget-exceeded flag via /exec. Now shares the
    exact same counters/flag as /exec: reserved (and count-checked) before
    the interpreter call runs, duration recorded (and seconds-checked)
    after it finishes.
    """
    if not req.code or not req.code.strip():
        raise HTTPException(status_code=400, detail="code is required")

    await main._reserve_session_exec_slot_or_raise(source="interpreter")

    timeout = min(max(1, req.timeout), main.INTERPRETER_MAX_EXEC_TIMEOUT_SECONDS)
    logger.info(f"[interpreter] exec: {req.code[:100]}...")

    _t0 = _time.monotonic()
    error_to_raise: Optional[HTTPException] = None
    response: Optional["main.InterpreterExecResponse"] = None
    async with _get_interpreter_lock():
        try:
            handle = await _get_or_spawn_interpreter_locked()
            response = await _interpreter_exec_now(handle, req.code, timeout)
        except (TimeoutError, RuntimeError) as exc:
            error_to_raise = HTTPException(status_code=502, detail=str(exc))

    # Recorded (and budget-checked) outside the interpreter lock -- a breach
    # here tears the session down via _reset_interpreter(), which itself
    # needs to acquire that same lock; holding it here would deadlock.
    duration_seconds = _time.monotonic() - _t0
    await main._record_session_exec_duration_or_raise(duration_seconds, source="interpreter")

    if error_to_raise is not None:
        raise error_to_raise
    return response


@router.post("/interpreter/reset", response_model=main.InterpreterResetResponse)
async def interpreter_reset():
    """Kill the persistent interpreter (if any); the next /interpreter/exec
    call starts a fresh one with an empty namespace."""
    await _reset_interpreter()
    return main.InterpreterResetResponse(status="reset")


@router.get("/interpreter/status", response_model=main.InterpreterStatusResponse)
async def interpreter_status():
    """Report whether a persistent interpreter is currently running."""
    async with _get_interpreter_lock():
        handle = main._interpreter_handle
        if handle is None or handle.proc.returncode is not None:
            return main.InterpreterStatusResponse(running=False)
        return main.InterpreterStatusResponse(
            running=True,
            started_at=handle.started_at,
            idle_seconds=round(_time.monotonic() - handle.last_used_at, 1),
        )
