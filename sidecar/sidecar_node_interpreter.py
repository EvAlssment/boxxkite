"""Persistent Node.js interpreter (one kept-alive process per session) and the
/node-interpreter/* routes.

The Node.js counterpart to ``sidecar_interpreter.py``'s Python interpreter --
same kept-alive-process shape, same request/response JSON-lines protocol over
stdin/stdout, same idle-timeout/output-cap accounting. Gated by
``BOXKITE_NODE_INTERPRETER_ENABLED`` (off by default): new attack surface (a
second kept-alive-interpreter code path), not a copy-paste of an
already-reviewed feature. See docs/NODE-INTERPRETER-DESIGN.md.

Follows the same module-split convention as ``sidecar_interpreter.py`` (split
out alongside it rather than staying in the original monolithic ``main.py``,
GitHub issue #71's refactor): the ``_node_interpreter_handle`` /
``_node_interpreter_lock`` state and all config/models remain owned by
``main`` and are referenced via ``main.<NAME>`` at call time so tests that
monkeypatch attributes on ``main`` still take effect;
``get_sandbox_pid``/``build_k8s_exec_command`` are likewise called via
``main.`` so monkeypatching is observed.

Persistence model: each request runs via Node's built-in `vm` module
(`vm.runInContext(code, persistentContext)`) against ONE `vm.createContext`
object created at driver startup and reused for every call -- this is what
makes `var`/bare-assignment globals AND top-level `let`/`const`/`class`
declarations all persist across separate calls, the same way they persist
across separate inputs typed into a real Node REPL or a browser devtools
console. This is deliberately NOT indirect eval (`(0, eval)(code)`):
indirect eval's `let`/`const`/`class` bindings live in a fresh, throwaway
lexical environment created per eval call and are discarded once that
call returns (only `var`/function declarations attach to something that
outlives the call) -- confirmed by hand against a real `node` binary
before choosing `vm` instead, not assumed from memory. `vm.createContext`
does not have this problem: repeated `vm.runInContext` calls against the
same context share one persistent top-level lexical environment, so
`let`/`const` survive exactly like `var` does. One side effect worth
calling out: re-declaring the same `let`/`const` name in a later call
raises a SyntaxError ("Identifier 'x' has already been declared") --
this is genuine, spec-defined top-level-lexical-scope behavior any real
JS REPL/devtools console has, not a driver bug; the fix from the agent's
side is the same one a human hitting this in devtools would use (reassign
without `let`/`const`, or pick a new name).

`vm.runInContext`'s own completion-value semantics (the same mechanism a
browser console uses to print "the value of what you just typed") gives
us the "return the last expression's value" behavior for free, without
the manual `ast.parse`-and-pop-the-last-expression surgery the Python
driver needs.

vm.createContext() is used purely for its persistent-lexical-environment
property here, NOT as a security/isolation boundary -- Node's own docs
are explicit that the vm module "is not a security mechanism for running
untrusted code." The actual isolation for this feature is the same as
everywhere else in this codebase: the OS-level sandbox (same UID, same
per-exec network namespace, same container) the Node subprocess itself
runs inside, identical to what bash_tool's `node -e` already gets.
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
from sidecar_interpreter import _INTERPRETER_READ_CHUNK_BYTES, _read_interpreter_response_line

logger = logging.getLogger("sidecar")

router = APIRouter()


_NODE_INTERPRETER_READY_SENTINEL = "__BOXKITE_NODE_INTERPRETER_READY__"

# The context object explicitly whitelists which Node-specific (non-ECMAScript)
# globals eval'd code can see -- standard built-ins (Object, Array, JSON,
# Math, Promise, RegExp, Error, ...) are provided automatically by
# vm.createContext() itself and don't need to be listed here. `require` is
# bound to the driver script's own require function (giving Node code the
# same practical capability level bash_tool's `node -e` already has -- full
# access to Node's built-in and installed modules -- not a new privilege,
# since this process runs with the same UID/network isolation as every
# other sandboxed command). `console` is explicitly constructed against the
# driver's real process.stdout/stderr (rather than relying on vm's own
# auto-provided default console, which is bound to a separate Console
# instance that does NOT route through a monkeypatched process.stdout.write
# -- confirmed by hand, not assumed) so runOne's stdout/stderr capture below
# actually catches console.log/error/warn/info output from eval'd code.
_NODE_INTERPRETER_DRIVER_SOURCE_TEMPLATE = """
const vm = require('vm');
const util = require('util');
const readline = require('readline');
const { Console } = require('console');

const sandboxContext = vm.createContext({
  console: new Console(process.stdout, process.stderr),
  require,
  process,
  Buffer,
  setTimeout, clearTimeout, setInterval, clearInterval, setImmediate, clearImmediate,
  URL, URLSearchParams, TextEncoder, TextDecoder,
  fetch: typeof fetch !== 'undefined' ? fetch : undefined,
});

process.stdout.write("BOXKITE_READY_SENTINEL\\n");

function runOne(code) {
  const originalStdoutWrite = process.stdout.write.bind(process.stdout);
  const originalStderrWrite = process.stderr.write.bind(process.stderr);
  let buf = '';
  const capture = function (chunk, encoding, callback) {
    const cb = typeof encoding === 'function' ? encoding : callback;
    try {
      buf += Buffer.isBuffer(chunk)
        ? chunk.toString(typeof encoding === 'string' ? encoding : 'utf8')
        : String(chunk);
    } catch (e) {
      // Ignore encoding edge cases -- never let capture itself throw.
    }
    if (typeof cb === 'function') cb();
    return true;
  };
  process.stdout.write = capture;
  process.stderr.write = capture;

  let resultRepr = null;
  let errorText = null;
  try {
    const value = vm.runInContext(code, sandboxContext);
    if (value !== undefined) {
      resultRepr = util.inspect(value, { depth: 4, maxArrayLength: 200, breakLength: 120 });
    }
  } catch (err) {
    errorText = (err && err.stack) ? String(err.stack) : String(err);
  } finally {
    process.stdout.write = originalStdoutWrite;
    process.stderr.write = originalStderrWrite;
  }
  return { stdout: buf, result: resultRepr, error: errorText };
}

const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', (rawLine) => {
  const line = rawLine.trim();
  if (!line) return;
  let request;
  try {
    request = JSON.parse(line);
  } catch (e) {
    process.stdout.write(JSON.stringify({ stdout: '', result: null, error: 'Invalid request: ' + e }) + '\\n');
    return;
  }
  const response = runOne(request.code || '');
  process.stdout.write(JSON.stringify(response) + '\\n');
});
"""

# BOXKITE_READY_SENTINEL is a literal placeholder token (not real JS -- it's
# substituted below), same convention _INTERPRETER_DRIVER_SOURCE uses.
_NODE_INTERPRETER_DRIVER_SOURCE = _NODE_INTERPRETER_DRIVER_SOURCE_TEMPLATE.replace(
    "BOXKITE_READY_SENTINEL", _NODE_INTERPRETER_READY_SENTINEL
)


class _NodeInterpreterHandle:
    """Wraps the persistent Node.js interpreter subprocess and its bookkeeping.

    Mirrors _InterpreterHandle -- see its docstring for why this isn't a
    dataclass.
    """

    def __init__(self, proc: "asyncio.subprocess.Process", script_path: str):
        self.proc = proc
        self.script_path = script_path
        self.started_at = datetime.now().isoformat()
        self.last_used_at = _time.monotonic()


def _get_node_interpreter_lock() -> asyncio.Lock:
    """Lazily create the Node interpreter lock in the active event loop.

    Mirrors _get_interpreter_lock -- a separate lock from the Python
    interpreter's own, so a call against one language never blocks on the
    other's spawn/exec/reset work.
    """
    if main._node_interpreter_lock is None:
        main._node_interpreter_lock = asyncio.Lock()
    return main._node_interpreter_lock


async def _kill_node_interpreter_handle(handle: "_NodeInterpreterHandle") -> None:
    """Terminate the Node interpreter subprocess and remove its driver script."""
    proc = handle.proc
    if proc.returncode is None:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            logger.warning("[node-interpreter] Process did not exit after kill()")
    try:
        os.remove(handle.script_path)
    except OSError:
        pass


async def _spawn_node_interpreter() -> "_NodeInterpreterHandle":
    """Start a fresh persistent Node.js interpreter process.

    Mirrors _spawn_interpreter's exact namespace-entry mechanism (same
    nsenter/unshare flags, same UID drop, same SAFE_EXEC_ENV, in both
    runtime modes). Memory is capped via `--max-old-space-size`
    instead of `ulimit -v` -- see NODE_INTERPRETER_MAX_MEMORY_MB's own
    comment in main.py for why a Python-style `ulimit -v` doesn't work for
    V8.
    """
    os.makedirs(main.TMP_DIR, exist_ok=True)
    script_path = os.path.join(main.TMP_DIR, f".boxkite-node-interpreter-{uuid4().hex}.js")
    with open(script_path, "w") as f:
        f.write(_NODE_INTERPRETER_DRIVER_SOURCE)
    os.chmod(script_path, 0o644)

    shell_command = (
        f"exec node --max-old-space-size={main.NODE_INTERPRETER_MAX_MEMORY_MB} {script_path}"
    )

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
    handle = _NodeInterpreterHandle(proc, script_path)

    try:
        ready_line = await asyncio.wait_for(
            proc.stdout.readline(), timeout=main.NODE_INTERPRETER_STARTUP_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        await _kill_node_interpreter_handle(handle)
        raise RuntimeError("Node interpreter did not start in time")

    if _NODE_INTERPRETER_READY_SENTINEL not in ready_line.decode("utf-8", errors="replace"):
        await _kill_node_interpreter_handle(handle)
        raise RuntimeError("Node interpreter failed to start")

    return handle


async def _get_or_spawn_node_interpreter_locked() -> "_NodeInterpreterHandle":
    """Return the live Node interpreter, respawning if absent, dead, or idle-expired.

    Caller MUST already hold _get_node_interpreter_lock() -- mirrors
    _get_or_spawn_interpreter_locked's own locking discipline.
    """
    handle = main._node_interpreter_handle
    still_fresh = (
        handle is not None
        and handle.proc.returncode is None
        and (_time.monotonic() - handle.last_used_at) <= main.NODE_INTERPRETER_IDLE_TIMEOUT_SECONDS
    )
    if still_fresh:
        return handle

    if handle is not None:
        await _kill_node_interpreter_handle(handle)
        main._node_interpreter_handle = None

    new_handle = await _spawn_node_interpreter()
    main._node_interpreter_handle = new_handle
    return new_handle


async def _reset_node_interpreter_locked() -> None:
    """Kill the current Node interpreter, if any. Caller MUST hold the lock."""
    handle = main._node_interpreter_handle
    main._node_interpreter_handle = None
    if handle is not None:
        await _kill_node_interpreter_handle(handle)


async def _reset_node_interpreter() -> None:
    """Kill the current Node interpreter (if any) so the next call starts fresh.

    Used by /node-interpreter/reset, by /configure's session-wipe path (a
    recycled pod must never hand a new tenant a still-live Node interpreter
    and its state from the previous tenant -- the same cross-tenant leak
    _reset_interpreter's own docstring describes for the Python interpreter),
    and by graceful shutdown.
    """
    async with _get_node_interpreter_lock():
        await _reset_node_interpreter_locked()


async def _reap_idle_node_interpreter() -> None:
    """Kill the persistent Node interpreter if it has been idle past its timeout.

    Mirrors _reap_idle_interpreter -- called from the same periodic sync
    loop cadence.
    """
    async with _get_node_interpreter_lock():
        handle = main._node_interpreter_handle
        if handle is None:
            return
        if (_time.monotonic() - handle.last_used_at) <= main.NODE_INTERPRETER_IDLE_TIMEOUT_SECONDS:
            return
        main._node_interpreter_handle = None

    logger.info(
        f"[node-interpreter] Idle for over {main.NODE_INTERPRETER_IDLE_TIMEOUT_SECONDS}s; "
        "killing interpreter"
    )
    await _kill_node_interpreter_handle(handle)


async def _node_interpreter_exec_now(
    handle: "_NodeInterpreterHandle", code: str, timeout: int
) -> "main.NodeInterpreterExecResponse":
    """Send one code snippet to a live Node interpreter handle and read its reply.

    Mirrors _interpreter_exec_now, including reusing
    _read_interpreter_response_line -- that helper is generic over any
    stdin/stdout-JSON-lines subprocess, not Python-specific.
    """
    payload = json.dumps({"code": code}) + "\n"
    handle.proc.stdin.write(payload.encode("utf-8"))
    await handle.proc.stdin.drain()

    line_read_cap = main.NODE_INTERPRETER_MAX_OUTPUT_BYTES * 4 + _INTERPRETER_READ_CHUNK_BYTES
    try:
        raw_line = await asyncio.wait_for(
            _read_interpreter_response_line(handle.proc.stdout, line_read_cap),
            timeout=timeout,
        )
    except asyncio.TimeoutError:
        await _kill_node_interpreter_handle(handle)
        main._node_interpreter_handle = None
        raise TimeoutError(f"Node interpreter call timed out after {timeout}s")

    if not raw_line:
        await _kill_node_interpreter_handle(handle)
        main._node_interpreter_handle = None
        raise RuntimeError("Node interpreter process exited unexpectedly")

    try:
        parsed = json.loads(raw_line.decode("utf-8", errors="replace"))
    except Exception as exc:
        await _kill_node_interpreter_handle(handle)
        main._node_interpreter_handle = None
        raise RuntimeError(f"Malformed Node interpreter response: {exc}")

    handle.last_used_at = _time.monotonic()

    stdout_text = str(parsed.get("stdout") or "")
    truncated = False
    stdout_bytes = stdout_text.encode("utf-8")
    if len(stdout_bytes) > main.NODE_INTERPRETER_MAX_OUTPUT_BYTES:
        stdout_text = stdout_bytes[: main.NODE_INTERPRETER_MAX_OUTPUT_BYTES].decode(
            "utf-8", errors="ignore"
        )
        truncated = True

    return main.NodeInterpreterExecResponse(
        stdout=stdout_text,
        result=parsed.get("result"),
        error=parsed.get("error"),
        truncated=truncated,
    )


@router.post("/node-interpreter/exec", response_model=main.NodeInterpreterExecResponse)
async def node_interpreter_exec(req: "main.NodeInterpreterExecRequest"):
    """
    Execute a code snippet against a persistent, kept-alive Node.js
    interpreter for the current session. See docs/NODE-INTERPRETER-DESIGN.md
    and this module's own docstring above.

    404s unless BOXKITE_NODE_INTERPRETER_ENABLED is set -- new attack
    surface, off by default, same posture as /pty-exec.

    Session exec budget (GitHub issue #122): shares the exact same
    counters/sticky flag as /exec, /interpreter/exec, and /process/start
    -- reserved (and count-checked) before the interpreter call runs,
    duration recorded (and seconds-checked) after it finishes, same shape
    as sidecar_interpreter.py's Python /interpreter/exec.
    """
    if not main.BOXKITE_NODE_INTERPRETER_ENABLED:
        raise HTTPException(status_code=404, detail="Node interpreter is not enabled on this deployment.")

    if not req.code or not req.code.strip():
        raise HTTPException(status_code=400, detail="code is required")

    await main._reserve_session_exec_slot_or_raise(source="node_interpreter")

    timeout = min(max(1, req.timeout), main.NODE_INTERPRETER_MAX_EXEC_TIMEOUT_SECONDS)
    logger.info(f"[node-interpreter] exec: {req.code[:100]}...")

    _t0 = _time.monotonic()
    error_to_raise: Optional[HTTPException] = None
    response: Optional["main.NodeInterpreterExecResponse"] = None
    async with _get_node_interpreter_lock():
        try:
            handle = await _get_or_spawn_node_interpreter_locked()
            response = await _node_interpreter_exec_now(handle, req.code, timeout)
        except (TimeoutError, RuntimeError) as exc:
            error_to_raise = HTTPException(status_code=502, detail=str(exc))

    # Recorded outside the node-interpreter lock -- same deadlock-avoidance
    # reason as sidecar_interpreter.py's /interpreter/exec.
    duration_seconds = _time.monotonic() - _t0
    await main._record_session_exec_duration_or_raise(duration_seconds, source="node_interpreter")

    if error_to_raise is not None:
        raise error_to_raise
    return response


@router.post("/node-interpreter/reset", response_model=main.NodeInterpreterResetResponse)
async def node_interpreter_reset():
    """Kill the persistent Node interpreter (if any); the next
    /node-interpreter/exec call starts a fresh one with empty state.

    404s unless BOXKITE_NODE_INTERPRETER_ENABLED is set, same as
    /node-interpreter/exec.
    """
    if not main.BOXKITE_NODE_INTERPRETER_ENABLED:
        raise HTTPException(status_code=404, detail="Node interpreter is not enabled on this deployment.")
    await _reset_node_interpreter()
    return main.NodeInterpreterResetResponse(status="reset")


@router.get("/node-interpreter/status", response_model=main.NodeInterpreterStatusResponse)
async def node_interpreter_status():
    """Report whether a persistent Node interpreter is currently running.

    404s unless BOXKITE_NODE_INTERPRETER_ENABLED is set, same as
    /node-interpreter/exec.
    """
    if not main.BOXKITE_NODE_INTERPRETER_ENABLED:
        raise HTTPException(status_code=404, detail="Node interpreter is not enabled on this deployment.")
    async with _get_node_interpreter_lock():
        handle = main._node_interpreter_handle
        if handle is None or handle.proc.returncode is not None:
            return main.NodeInterpreterStatusResponse(running=False)
        return main.NodeInterpreterStatusResponse(
            running=True,
            started_at=handle.started_at,
            idle_seconds=round(_time.monotonic() - handle.last_used_at, 1),
        )
