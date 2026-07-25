"""Background process registry (/process/*) and the network-ingress preview
proxy (/preview/{port}/...).

Split out of the original monolithic ``main.py`` (GitHub issue #71) as a pure
mechanical refactor -- no behavior change at the time of that split. The
registry state (``_process_registry``, ``_process_registry_lock``,
``_exposed_ports``) and all config/models remain owned by ``main`` and are
referenced via ``main.<NAME>``; ``_spawn_background_process`` is called via
``main.`` because tests monkeypatch it there.

Process-group teardown (``_signal_process_group``) and the startup orphan
sweep (``_sweep_orphaned_background_processes``) were added after the split
(GitHub issue #76) -- see their own docstrings and
docs/PROCESS-SESSIONS-DESIGN.md section 2(b) for why killing only the
tracked PID is not sufficient for K8s-mode background processes.
"""

import asyncio
import logging
import os
import signal
from datetime import datetime
from typing import Optional
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response, StreamingResponse

import main

logger = logging.getLogger("sidecar")

router = APIRouter()


# ============================================================================
# Background process registry (/process/*)
#
# See docs/PROCESS-SESSIONS-DESIGN.md. Unlike every other route in this file,
# /process/start does not await its subprocess to completion — it hands back
# a `process_id` immediately and keeps the asyncio.subprocess.Process handle
# alive in `_process_registry` so later HTTP calls (/output, /input, /stop)
# can interact with the same OS process across multiple requests. This is the
# first genuinely new in-memory-state-that-outlives-a-request pattern in this
# codebase, so process teardown is deliberately explicit and paranoid: see
# `_kill_all_processes()`, called from both `/configure` (pod recycle/claim)
# and graceful shutdown, closing the cross-tenant leak this feature would
# otherwise introduce (a process — and its buffered stdout — started by one
# tenant, still alive or still readable, once a recycled pod is claimed by a
# different tenant).
# ============================================================================


def _get_process_registry_lock() -> asyncio.Lock:
    """Lazily create the registry lock in the active event loop (same pattern
    as `_get_flush_lock` above)."""
    if main._process_registry_lock is None:
        main._process_registry_lock = asyncio.Lock()
    return main._process_registry_lock


class ProcessHandle:
    """Tracks one background process spawned via `/process/start`.

    Combines stdout+stderr into a single bounded ring buffer (see
    PROCESS_OUTPUT_MAX_BYTES) rather than two separate streams — simpler
    offset/truncation bookkeeping, and matches the API shape in
    docs/PROCESS-SESSIONS-DESIGN.md section 3 (a single `stdout_chunk`
    field).
    """

    def __init__(
        self,
        process_id: str,
        proc: "asyncio.subprocess.Process",
        command: str,
        description: Optional[str],
        max_runtime_seconds: int,
        expose_port: Optional[int] = None,
    ):
        self.process_id = process_id
        self.proc = proc
        self.command = command
        self.description = description
        self.max_runtime_seconds = max_runtime_seconds
        self.expose_port = expose_port
        self.started_at = datetime.now()
        self.status = "running"  # running | exited | stopped | killed
        self.exit_code: Optional[int] = None
        self.buffer = bytearray()
        self.buffer_start_offset = 0  # cumulative offset of buffer[0]
        self.total_bytes_written = 0
        self.lock = asyncio.Lock()
        self.reader_task: Optional[asyncio.Task] = None
        self.watchdog_task: Optional[asyncio.Task] = None

    def append_output(self, chunk: bytes) -> None:
        """Append to the ring buffer, dropping the oldest bytes once over cap.

        Caller must hold `self.lock`.
        """
        self.buffer.extend(chunk)
        self.total_bytes_written += len(chunk)
        if len(self.buffer) > main.PROCESS_OUTPUT_MAX_BYTES:
            drop = len(self.buffer) - main.PROCESS_OUTPUT_MAX_BYTES
            del self.buffer[:drop]
            self.buffer_start_offset += drop

    def read_since(self, since_offset: int) -> tuple[bytes, bool]:
        """Return (chunk, truncated) for bytes from `since_offset` onward.

        `truncated` is True when `since_offset` points at bytes the ring
        buffer has already dropped -- the caller must not assume
        `since_offset=0` always returns everything ever written. Caller must
        hold `self.lock`.
        """
        since_offset = max(0, since_offset)
        if since_offset < self.buffer_start_offset:
            return bytes(self.buffer), True
        start = since_offset - self.buffer_start_offset
        return bytes(self.buffer[start:]), False


async def _spawn_background_process(
    command: str, *, expose_network: bool = False
) -> "asyncio.subprocess.Process":
    """Spawn a background process the same way exec_in_sandbox starts one
    (same nsenter/build_k8s_exec_command, same UID drop, same SAFE_EXEC_ENV,
    same per-call fresh network namespace) but with a stdin pipe and
    stdout+stderr merged, and without awaiting completion.

    `expose_network=True` (only ever set when the caller supplied
    ProcessStartRequest.expose_port -- see docs/NETWORK-INGRESS-DESIGN.md)
    skips the fresh per-exec network namespace so this process's listening
    port stays reachable from the sidecar's own loopback for /preview
    proxying.

    SECURITY: see exec_in_sandbox's docstring -- same UID drop, same
    sanitized environment, same caveats. No caller-supplied `env` is ever
    accepted (matches ExecRequest's own comment on this).

    Restart/orphan survival (docs/PROCESS-SESSIONS-DESIGN.md section 2(b)):
    `start_new_session=True` makes the spawned nsenter command a new
    process-group leader, so nsenter's own internal fork (required to
    actually enter the target PID namespace with `-p`) inherits that same
    group instead of silently escaping a group-wide kill -- verified
    directly that without this, killing only the tracked nsenter PID left
    the real sandboxed command running untouched. See
    `_signal_process_group` below, used by every teardown path instead of
    `proc.kill()`/`proc.terminate()` directly. The marker env var lets a
    freshly-restarted sidecar recognize and reap this process's whole group
    if it survives a hard crash/OOM-kill of the sidecar itself (see
    `_sweep_orphaned_background_processes`) -- both runtime modes share a
    PID namespace with the sandbox container now (deploy/docker-compose.yml's
    `pid: "container:sandbox"`), so this sidecar's own /proc sees the process
    in either mode, not just K8s.
    """
    sandbox_pid = main.get_sandbox_pid()
    if not sandbox_pid:
        raise RuntimeError("Failed to find sandbox process")
    cmd = main.build_k8s_exec_command(sandbox_pid, command, skip_network_isolation=expose_network)
    env = dict(main.SAFE_EXEC_ENV)
    env[main.BACKGROUND_PROCESS_MARKER_ENV] = main.BACKGROUND_PROCESS_MARKER_VALUE

    return await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )


async def _process_reader_loop(handle: "ProcessHandle") -> None:
    """Continuously drain the process's merged stdout/stderr into its ring
    buffer until EOF, then record its exit code.

    Unlike `_read_stream_bounded` (used by one-shot /exec), this never kills
    the process for producing too much output -- it drops the oldest
    buffered bytes instead, since a background process is explicitly meant
    to keep running past what any single poll call has read.
    """
    stream = handle.proc.stdout
    try:
        while stream is not None:
            chunk = await stream.read(main._EXEC_READ_CHUNK_SIZE)
            if not chunk:
                break
            async with handle.lock:
                handle.append_output(chunk)
    except Exception as e:
        logger.error(f"[process:{handle.process_id}] reader error: {e}")

    returncode = await handle.proc.wait()
    async with handle.lock:
        if handle.status == "running":
            handle.status = "exited"
        handle.exit_code = returncode

    if handle.watchdog_task is not None:
        handle.watchdog_task.cancel()

    async with _get_process_registry_lock():
        _release_exposed_port(handle)


def _signal_process_group(proc: "asyncio.subprocess.Process", sig: int) -> None:
    """Send `sig` to `proc`'s whole process group, not just `proc` itself.

    Required for background processes: `_spawn_background_process` wraps
    the real command in `nsenter -t <pid> -m -p ...`, and nsenter forks
    internally to actually enter the target PID namespace (`-p` cannot be
    applied to the calling process itself, only to a child it creates after
    the fork) -- so `proc` (the tracked `asyncio.subprocess.Process`) is
    nsenter's OWN pid, one level above the real sandboxed command. This was
    verified directly against real containers sharing a PID namespace
    (docs/PROCESS-SESSIONS-DESIGN.md section 2(b)): signalling only
    `proc.pid` killed nsenter and left the actual command it wrapped alive
    and running, reparented, untouched. `_spawn_background_process` sets
    `start_new_session=True` so nsenter and everything it forks share one
    process group -- `killpg` on that group reaches all of them in one
    signal. Applies the same way in both runtime modes now that both share
    a PID namespace with the sandbox container.

    Falls back to signalling `proc.pid` directly if the group can't be
    resolved (process already gone).

    SAFETY: never `killpg` the CALLER's own process group. This matters
    whenever a spawned process was NOT actually given its own session (a
    test double that skips `start_new_session=True`, or any future
    regression that does the same) -- without this check, `os.getpgid(pid)`
    would resolve to the sidecar's own group (children inherit their
    parent's process group by default), and `killpg` would SIGKILL the
    sidecar itself along with everything else sharing that group. Verified
    this is not a theoretical concern: it reproduced immediately against
    this repo's own test harness before this guard was added. Falls back to
    signalling just `proc.pid` in that case -- weaker, but never
    self-destructive.
    """
    my_pgid = os.getpgid(0)
    try:
        pgid = os.getpgid(proc.pid)
    except (ProcessLookupError, PermissionError):
        return
    if pgid == my_pgid:
        try:
            proc.send_signal(sig)
        except ProcessLookupError:
            pass
        return
    try:
        os.killpg(pgid, sig)
        return
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    try:
        proc.send_signal(sig)
    except ProcessLookupError:
        pass


async def _process_watchdog(handle: "ProcessHandle") -> None:
    """Force-kill a background process once it exceeds its own
    `max_runtime_seconds` ceiling -- the async equivalent of /exec's
    `asyncio.wait_for(..., timeout=...)`, since this process is never
    awaited synchronously."""
    try:
        await asyncio.sleep(handle.max_runtime_seconds)
    except asyncio.CancelledError:
        return

    if handle.proc.returncode is None:
        logger.warning(
            f"[process:{handle.process_id}] exceeded max_runtime_seconds="
            f"{handle.max_runtime_seconds}s; killing"
        )
        async with handle.lock:
            handle.status = "killed"
        _signal_process_group(handle.proc, signal.SIGKILL)


async def _stop_process(handle: "ProcessHandle") -> None:
    """SIGTERM, grace period, then SIGKILL if still alive.

    Marks `status = "stopped"` up front (before terminate/kill), not after
    `proc.wait()` resolves -- `_process_reader_loop` also awaits `proc.wait()`
    and would otherwise race this function to set the final status (it only
    overwrites `status` when it's still "running", so setting "stopped"
    first makes that race harmless instead of depending on which coroutine
    happens to be scheduled first).
    """
    if handle.watchdog_task is not None:
        handle.watchdog_task.cancel()

    async with handle.lock:
        if handle.status == "running":
            handle.status = "stopped"

    if handle.proc.returncode is None:
        _signal_process_group(handle.proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(handle.proc.wait(), timeout=main.PROCESS_STOP_GRACE_PERIOD_SECONDS)
        except asyncio.TimeoutError:
            _signal_process_group(handle.proc, signal.SIGKILL)
            await handle.proc.wait()

    async with handle.lock:
        handle.exit_code = handle.proc.returncode

    if handle.reader_task is not None:
        try:
            await asyncio.wait_for(handle.reader_task, timeout=main.PROCESS_STOP_GRACE_PERIOD_SECONDS)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass


def _release_exposed_port(handle: "ProcessHandle") -> None:
    """Remove this process's port from _exposed_ports, if it registered one.

    Caller must hold the process registry lock. Only pops the mapping if it
    still points at THIS process_id -- a defensive check against a
    theoretical future bug where a port got reassigned, not something that
    can currently happen given /process/start's own uniqueness check.
    """
    if handle.expose_port is None:
        return
    if main._exposed_ports.get(handle.expose_port) == handle.process_id:
        del main._exposed_ports[handle.expose_port]


def _get_process_or_404(process_id: str) -> "ProcessHandle":
    handle = main._process_registry.get(process_id)
    if handle is None:
        raise HTTPException(status_code=404, detail=f"Process {process_id} not found")
    return handle


async def _kill_all_processes() -> int:
    """SIGKILL every tracked background process and clear the registry.

    Mandatory before any pod-identity change (a recycled pod being claimed by
    a different tenant's session) and before graceful shutdown -- see
    docs/PROCESS-SESSIONS-DESIGN.md sections 2(b)/5. Without this, a
    background process (and its buffered output, which can contain anything
    the previous tenant's agent printed) started by one session could still
    be running, or still be readable via /process/{id}/output, after the pod
    is recycled to a different tenant.

    Returns the number of processes that were still alive at the time of the
    call.
    """
    lock = _get_process_registry_lock()
    async with lock:
        handles = list(main._process_registry.values())
        main._process_registry.clear()
        # Cross-tenant leak closure (see this function's own docstring):
        # a stale port->process_id mapping must never survive a pod
        # recycle, or a new tenant's /preview call could be routed to (or
        # 409-blocked by) a port the previous tenant registered.
        main._exposed_ports.clear()

    killed = 0
    for handle in handles:
        if handle.watchdog_task is not None:
            handle.watchdog_task.cancel()
        if handle.proc.returncode is None:
            _signal_process_group(handle.proc, signal.SIGKILL)
            killed += 1
        if handle.reader_task is not None:
            handle.reader_task.cancel()
        handle.status = "killed"
    return killed


def _sweep_orphaned_background_processes() -> int:
    """Kill any background-process descendants left over from a previous,
    now-dead incarnation of this sidecar process.

    See docs/PROCESS-SESSIONS-DESIGN.md section 2(b): `shutdown_event`'s
    graceful `_kill_all_processes()` call only runs on a clean shutdown --
    an OOM-kill, a segfault, or any other hard crash of the sidecar's own
    process never reaches it at all. This repo's own tested experiment
    (real containers, real `nsenter`, a real hard SIGKILL of the "sidecar"
    container) confirmed the resulting process keeps running: reparented,
    fully alive (not a zombie), inside the pod's shared PID namespace
    (`shareProcessNamespace: true`) -- invisible to a freshly-started
    sidecar's own (empty) `_process_registry`.

    This runs once at startup, before the periodic sync loop starts, and
    finds those survivors by their marker env var
    (BACKGROUND_PROCESS_MARKER_ENV, injected only into K8s-mode
    `/process/start` spawns -- see `_spawn_background_process`) via
    `/proc/<pid>/environ`, rather than any marker file the sandboxed
    process could read or forge for an unrelated PID. A freshly-restarted
    sidecar has spawned nothing of its own yet, so any match found here can
    only be a leftover from a prior incarnation, never something legitimately
    in flight.

    Only when SANDBOX_PROCESS_STARTUP_SWEEP_ENABLED (default on). Runs the
    same way in both runtime modes -- both share a PID namespace with the
    sandbox container (deploy/docker-compose.yml's `pid: "container:sandbox"`
    in compose mode), so this sidecar's own /proc sees the marker either way.

    Returns the number of process groups killed.
    """
    if not main.SANDBOX_PROCESS_STARTUP_SWEEP_ENABLED:
        return 0

    try:
        candidate_pids = [int(name) for name in os.listdir("/proc") if name.isdigit()]
    except OSError as e:
        logger.warning(f"[startup] Orphan-process sweep could not list /proc: {e}")
        return 0

    my_pid = os.getpid()
    marker = f"{main.BACKGROUND_PROCESS_MARKER_ENV}={main.BACKGROUND_PROCESS_MARKER_VALUE}".encode()
    killed_pgids: set[int] = set()

    for pid in candidate_pids:
        if pid == my_pid:
            continue
        try:
            with open(f"/proc/{pid}/environ", "rb") as f:
                environ = f.read()
        except (FileNotFoundError, ProcessLookupError, PermissionError, OSError):
            continue
        if marker not in environ.split(b"\x00"):
            continue

        try:
            pgid = os.getpgid(pid)
        except ProcessLookupError:
            continue
        if pgid in killed_pgids:
            continue

        try:
            os.killpg(pgid, signal.SIGKILL)
        except ProcessLookupError:
            continue
        except PermissionError as e:
            logger.warning(f"[startup] Could not reap orphaned process group pgid={pgid}: {e}")
            continue

        killed_pgids.add(pgid)
        logger.warning(
            f"[startup] Reaped orphaned background process pid={pid} pgid={pgid} "
            "left over from a previous sidecar incarnation (see "
            "docs/PROCESS-SESSIONS-DESIGN.md section 2(b))"
        )

    return len(killed_pgids)


@router.post("/process/start", response_model=main.ProcessStartResponse, status_code=201)
async def process_start(req: main.ProcessStartRequest):
    """
    Start a background process in the sandbox container, tracked across
    multiple HTTP calls until it exits or is stopped.

    Distinct from /exec: /exec is one-shot request/response, bounded by its
    own timeout. This spawns the same way /exec does (same nsenter/UID-drop/
    SAFE_EXEC_ENV machinery) but does not await completion -- the caller
    gets a `process_id` back immediately and polls /process/{id}/output for
    progress. See docs/PROCESS-SESSIONS-DESIGN.md.

    Session exec budget (GitHub issue #122): this route ships enabled by
    default, same as bash_tool -- a security review found the budget was
    originally wired into /exec only, so an agent spinning up background
    processes in a loop spent zero budget and was never throttled, and
    could keep starting new ones completely unobstructed even after
    already tripping the sticky budget-exceeded flag via /exec. Each
    successful start now consumes one exec-count unit against the same
    counters/flag /exec uses. It does NOT contribute to the cumulative
    exec-SECONDS total -- this route returns before its spawned process
    finishes, so there is no synchronous call duration to measure the way
    there is for /exec and /interpreter/exec; a long-running background
    process's own wall-clock runtime is bounded separately by
    max_runtime_seconds/PROCESS_MAX_RUNTIME_SECONDS_CEILING instead.
    """
    await main._reserve_session_exec_slot_or_raise(source="process_start")

    if req.max_runtime_seconds <= 0 or req.max_runtime_seconds > main.PROCESS_MAX_RUNTIME_SECONDS_CEILING:
        raise HTTPException(
            status_code=400,
            detail=(
                f"max_runtime_seconds must be between 1 and "
                f"{main.PROCESS_MAX_RUNTIME_SECONDS_CEILING}"
            ),
        )

    if req.expose_port is not None:
        if req.expose_port == main.SIDECAR_PORT or not (main.PREVIEW_PORT_MIN <= req.expose_port <= main.PREVIEW_PORT_MAX):
            raise HTTPException(
                status_code=400,
                detail=(
                    f"expose_port must be between {main.PREVIEW_PORT_MIN} and {main.PREVIEW_PORT_MAX} "
                    f"and cannot be the sidecar's own port ({main.SIDECAR_PORT})"
                ),
            )

    lock = _get_process_registry_lock()
    async with lock:
        active = sum(1 for h in main._process_registry.values() if h.status == "running")
        if active >= main.SANDBOX_MAX_BACKGROUND_PROCESSES:
            raise HTTPException(
                status_code=429,
                detail=(
                    f"Session already has {active} background process(es) running "
                    f"(max {main.SANDBOX_MAX_BACKGROUND_PROCESSES}). Stop one before "
                    "starting another."
                ),
            )

        if req.expose_port is not None and req.expose_port in main._exposed_ports:
            raise HTTPException(
                status_code=409,
                detail=f"Port {req.expose_port} is already exposed by another tracked process",
            )

        try:
            proc = await main._spawn_background_process(
                req.command, expose_network=req.expose_port is not None
            )
        except Exception as e:
            logger.error(f"[process:start] Failed to spawn: {e}")
            raise HTTPException(status_code=500, detail=f"Failed to start process: {e}")

        process_id = f"proc_{uuid4().hex}"
        handle = ProcessHandle(
            process_id=process_id,
            proc=proc,
            command=req.command,
            description=req.description,
            max_runtime_seconds=req.max_runtime_seconds,
            expose_port=req.expose_port,
        )
        handle.reader_task = asyncio.create_task(_process_reader_loop(handle))
        handle.watchdog_task = asyncio.create_task(_process_watchdog(handle))
        main._process_registry[process_id] = handle
        if req.expose_port is not None:
            main._exposed_ports[req.expose_port] = process_id

    logger.info(f"[process:start] {process_id}: {req.command[:100]}")
    return main.ProcessStartResponse(
        process_id=process_id,
        status=handle.status,
        started_at=handle.started_at.isoformat(),
    )


@router.get("/process/{process_id}/output", response_model=main.ProcessOutputResponse)
async def process_output(process_id: str, since_offset: int = 0):
    """
    Poll a background process's output since a given byte offset.

    Polling-style, not streaming -- see docs/PROCESS-SESSIONS-DESIGN.md
    section 3 for why (SSE streaming is an explicit, separate follow-up
    phase, not part of this route). The byte offset lets a caller catch up
    after a disconnect without re-reading from the start.
    """
    handle = _get_process_or_404(process_id)
    async with handle.lock:
        chunk, truncated = handle.read_since(since_offset)
        next_offset = handle.total_bytes_written
        status = handle.status
        exit_code = handle.exit_code

    return main.ProcessOutputResponse(
        status=status,
        stdout_chunk=chunk.decode("utf-8", errors="replace"),
        next_offset=next_offset,
        truncated=truncated,
        exit_code=exit_code,
    )


@router.post("/process/{process_id}/input", response_model=main.ProcessInputResponse)
async def process_input(process_id: str, req: main.ProcessInputRequest):
    """Write to a tracked background process's stdin pipe."""
    handle = _get_process_or_404(process_id)
    if handle.proc.stdin is None or handle.proc.returncode is not None:
        raise HTTPException(status_code=409, detail=f"Process {process_id} is not running")

    payload = req.data.encode("utf-8")
    try:
        handle.proc.stdin.write(payload)
        await handle.proc.stdin.drain()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to write to process stdin: {e}")

    return main.ProcessInputResponse(bytes_written=len(payload))


@router.post("/process/{process_id}/stop", response_model=main.ProcessStopResponse)
async def process_stop(process_id: str):
    """Stop a tracked background process: SIGTERM, grace period, SIGKILL."""
    handle = _get_process_or_404(process_id)
    await _stop_process(handle)
    async with _get_process_registry_lock():
        _release_exposed_port(handle)
    return main.ProcessStopResponse(status=handle.status, exit_code=handle.exit_code)


@router.get("/process", response_model=main.ProcessListResponse)
async def process_list():
    """List every background process currently tracked for this pod's session."""
    processes = [
        main.ProcessInfo(
            process_id=handle.process_id,
            command=handle.command,
            description=handle.description,
            status=handle.status,
            started_at=handle.started_at.isoformat(),
            exit_code=handle.exit_code,
            expose_port=handle.expose_port,
        )
        for handle in main._process_registry.values()
    ]
    return main.ProcessListResponse(processes=processes)


@router.post("/process/kill-all", response_model=main.ProcessKillAllResponse)
async def process_kill_all():
    """
    SIGKILL every tracked background process and clear the registry.

    Called by SandboxManager.destroy_session()/_recycle_pod_via_k8s() before
    the existing /configure wipe call, and internally by /configure itself
    as defense in depth -- see _kill_all_processes()'s docstring for why this
    is mandatory, not optional hardening.
    """
    killed = await _kill_all_processes()
    return main.ProcessKillAllResponse(killed=killed)


# ============================================================================
# Network ingress preview (/preview/{port}/...) — see
# docs/NETWORK-INGRESS-DESIGN.md.
#
# Reverse-proxies an HTTP request to `http://127.0.0.1:{port}/{path}` inside
# the pod's own network namespace. Auth for this route is the exact same
# `X-Sidecar-Auth-Token` shared secret enforced by `enforce_sidecar_auth` on
# every other route in this file -- this endpoint is never reachable
# directly from outside the cluster (NetworkPolicy ingress into the pod
# stays default-deny), only the control-plane calls it, after the
# control-plane's own signed, time-limited preview-URL check has already
# passed (see control-plane's routers/sandboxes.py preview routes). The
# sidecar itself does not need to know anything about that signing scheme.
#
# Only proxies to a port a live tracked process actually registered via
# ProcessStartRequest.expose_port (`_exposed_ports`) -- never an arbitrary
# port, which would otherwise turn this into a port-scanner into the pod's
# own network namespace.
# ============================================================================


def _filtered_proxy_headers(raw_headers) -> dict:
    """Drop hop-by-hop headers and the sidecar's own auth header before
    forwarding in either direction across the proxy boundary."""
    return {
        key: value
        for key, value in raw_headers.items()
        if key.lower() not in main._PREVIEW_HOP_BY_HOP_HEADERS and key.lower() != main.SIDECAR_AUTH_HEADER.lower()
    }


# A dedicated indirection to time.monotonic() rather than calling `main._time`
# directly, so tests can monkeypatch just this one call site to simulate the
# max-duration safety valve tripping deterministically -- monkeypatching the
# real time.monotonic() globally would also perturb asyncio's own internal
# event-loop clock.
def _preview_stream_monotonic() -> float:
    return main._time.monotonic()


async def _stream_upstream_body(
    client: httpx.AsyncClient, upstream_response: httpx.Response, *, port: int
):
    """Yield the upstream response body chunk by chunk, enforcing the
    optional total-byte cap (main.PREVIEW_MAX_RESPONSE_BYTES, off by default)
    and the overall wall-clock cap (main.PREVIEW_STREAM_MAX_SECONDS) as the
    two remaining safety valves for what is otherwise unbounded true
    streaming. Always closes the upstream response and the per-request
    client, even if the caller disconnects mid-stream or a cap trips.
    """
    sent = 0
    deadline = main._preview_stream_monotonic() + main.PREVIEW_STREAM_MAX_SECONDS
    try:
        async for chunk in upstream_response.aiter_bytes():
            if main.PREVIEW_MAX_RESPONSE_BYTES and sent + len(chunk) > main.PREVIEW_MAX_RESPONSE_BYTES:
                remaining = main.PREVIEW_MAX_RESPONSE_BYTES - sent
                if remaining > 0:
                    yield chunk[:remaining]
                logger.warning(
                    f"[preview:{port}] streamed response exceeded "
                    f"{main.PREVIEW_MAX_RESPONSE_BYTES} bytes; truncating"
                )
                return
            sent += len(chunk)
            yield chunk
            if main._preview_stream_monotonic() > deadline:
                logger.warning(
                    f"[preview:{port}] streamed response exceeded "
                    f"{main.PREVIEW_STREAM_MAX_SECONDS}s; aborting"
                )
                return
    finally:
        await upstream_response.aclose()
        await client.aclose()


@router.api_route(
    "/preview/{port}/{path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
)
async def preview_proxy(port: int, path: str, request: Request) -> Response:
    """Reverse-proxy one HTTP request to a registered preview port.

    TRUE streaming: the upstream response is forwarded to the caller as it
    arrives (StreamingResponse over httpx's `aiter_bytes()`), never fully
    buffered in memory first -- see docs/NETWORK-INGRESS-DESIGN.md's "no true
    streaming" follow-up, closed by this change. The status code and headers
    are still known synchronously before any body bytes are sent, since
    httpx's `stream=True` send only waits for the response headers, not the
    body.
    """
    process_id = main._exposed_ports.get(port)
    if process_id is None or process_id not in main._process_registry:
        raise HTTPException(status_code=404, detail=f"No process is exposing port {port}")

    handle = main._process_registry[process_id]
    if handle.status != "running":
        raise HTTPException(status_code=502, detail=f"Process exposing port {port} is not running")

    upstream_url = f"http://127.0.0.1:{port}/{path}"
    body = await request.body()
    headers = _filtered_proxy_headers(request.headers)

    # Not a context manager: the client and the streamed response must both
    # stay open past this function's return, for as long as StreamingResponse
    # is still draining the body iterator below. `_stream_upstream_body`'s
    # `finally` clause is what actually closes both, once streaming ends
    # (normally, on a cap trip, or on caller disconnect).
    client = httpx.AsyncClient(timeout=main.PREVIEW_UPSTREAM_TIMEOUT_SECONDS)
    try:
        upstream_request = client.build_request(
            request.method,
            upstream_url,
            params=request.query_params,
            headers=headers,
            content=body,
        )
        upstream_response = await client.send(
            upstream_request, stream=True, follow_redirects=False
        )
    except httpx.RequestError as e:
        await client.aclose()
        logger.warning(f"[preview:{port}] Upstream request failed: {e}")
        raise HTTPException(
            status_code=502, detail=f"Preview upstream on port {port} is unreachable: {e}"
        )

    response_headers = _filtered_proxy_headers(upstream_response.headers)
    return StreamingResponse(
        _stream_upstream_body(client, upstream_response, port=port),
        status_code=upstream_response.status_code,
        headers=response_headers,
        media_type=upstream_response.headers.get("content-type"),
    )
