"""Command execution in the sandbox (nsenter / docker exec) and the /exec and
/tool-call routes.

Split out of the original monolithic ``main.py`` (GitHub issue #71) as a pure
mechanical refactor -- no behavior change. Shared configuration, state, and
the request/response models remain owned by ``main`` and are referenced via
``main.<NAME>``. Functions that tests monkeypatch on ``main`` (``get_sandbox_pid``,
``exec_in_sandbox``, ``_get_secret_value``, ...) are always called through
``main.`` so a patch is observed regardless of where the caller lives.
"""

import asyncio
import logging
import os
import subprocess
from typing import Optional

from fastapi import APIRouter

import main

logger = logging.getLogger("sidecar")

router = APIRouter()


# ============================================================================
# Exec via nsenter (for bash_tool)
# ============================================================================

def get_sandbox_pid() -> Optional[int]:
    """Get PID of sandbox container's init process.

    Works identically in K8s (containers in one pod always share a PID
    namespace) and compose mode (deploy/docker-compose.yml gives the sidecar
    `pid: "container:sandbox"` specifically so this pgrep-based lookup finds
    the same process it would in a real pod) -- both share a PID namespace
    with the sandbox container, so both can see its init process this way.
    """
    # Find the sandbox process. The sandbox runs "tail -f /dev/null" as PID 1
    try:
        result = subprocess.run(
            ["pgrep", "-f", "tail -f /dev/null"],
            capture_output=True,
            text=True,
            timeout=5
        )
        if result.returncode == 0:
            pids = result.stdout.strip().split('\n')
            # Return the first PID that's not us
            my_pid = os.getpid()
            for pid in pids:
                if pid and int(pid) != my_pid:
                    return int(pid)
    except Exception as e:
        logger.error(f"Failed to find sandbox PID: {e}")
    return None


def build_k8s_exec_command(
    sandbox_pid: int, command: str, *, skip_network_isolation: bool = False
) -> list[str]:
    """Build the K8s exec command used for generated-code execution.

    `skip_network_isolation` is a narrow, scoped override used ONLY by
    /process/start's `expose_port` path (see docs/NETWORK-INGRESS-DESIGN.md):
    a background process that wants its listening port reachable for a
    preview URL must stay in the pod's own shared network namespace (the one
    the sidecar container itself is in), instead of getting the fresh, empty
    per-exec namespace every other exec'd command gets. This does NOT change
    the pod's NetworkPolicy egress/ingress posture -- it only opts one
    specific, explicitly-requested process out of the *additional*
    per-exec namespace isolation layer, back to the pod-level isolation
    every container in the pod already has. See the SECURITY comment on
    ProcessStartRequest.expose_port below for the full reasoning.
    """
    nsenter_cmd = [
        "nsenter",
        "-t", str(sandbox_pid),
        "-m", "-p",  # Mount and PID namespaces
        "--setuid", str(main.SANDBOX_UID),  # SECURITY: Drop to sandbox user
        "--setgid", str(main.SANDBOX_GID),  # SECURITY: Drop to sandbox group
        "--", "sh", "-c", command,
    ]

    if skip_network_isolation or not main.SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED:
        return nsenter_cmd

    # SECURITY: create the empty network namespace before nsenter. If unshare
    # runs after entering the sandbox mount namespace, the sidecar's unshare
    # binary is no longer visible.
    return ["unshare", "-n", *nsenter_cmd]


async def _read_stream_bounded(
    stream: "asyncio.StreamReader | None",
    max_bytes: int,
    proc: "asyncio.subprocess.Process",
) -> bytes:
    """Drain a subprocess stream incrementally, capping memory at `max_bytes`.

    Unlike `Process.communicate()`, this never buffers output past the cap
    before truncating — the moment the cap is hit, the process is killed
    instead of being allowed to keep writing into memory.
    """
    if stream is None:
        return b""

    chunks: list[bytes] = []
    total = 0
    while True:
        chunk = await stream.read(main._EXEC_READ_CHUNK_SIZE)
        if not chunk:
            break

        remaining = max_bytes - total
        if remaining <= 0:
            if proc.returncode is None:
                proc.kill()
            break

        if len(chunk) > remaining:
            chunks.append(chunk[:remaining])
            if proc.returncode is None:
                proc.kill()
            break

        chunks.append(chunk)
        total += len(chunk)

    return b"".join(chunks)


async def exec_in_sandbox(
    command: str,
    timeout: int = 30,
    extra_env: Optional[dict[str, str]] = None,
) -> tuple[int, str, str]:
    """
    Execute command in sandbox container, via nsenter + privilege dropping --
    identically in K8s and compose mode (see get_sandbox_pid's docstring for
    why compose mode can use the same nsenter path as K8s). When enabled,
    the command runs in a fresh network namespace so it cannot reach IMDS,
    private endpoints, the sidecar's own HTTP port, or the public internet
    directly -- this used to be K8s-only (compose fell back to `docker exec`,
    which always joins the target container's *existing* network namespace
    with no way to give it a fresh one per call); both modes now go through
    build_k8s_exec_command so this isolation is no longer compose-only-absent.

    SECURITY:
    - Commands run as UID 1001 (sandbox user), NOT as root
    - Environment is sanitized - only SAFE_EXEC_ENV vars (plus any
      server-resolved `extra_env`, never caller-supplied literal values --
      see ExecRequest.secret_env) are passed
    - No sidecar credentials (Azure/S3) are leaked to the subprocess
    - No arbitrary caller-supplied environment variables are accepted here
      (see the comment on ExecRequest) — only the fixed SAFE_EXEC_ENV, plus
      `extra_env` values this function's own caller already resolved
      server-side from a granted secret name, are ever passed to the
      exec'd process.
    """
    # The exec'd command inherits this subprocess's env directly, so merging
    # into `env=` below is sufficient (no argv exposure).
    sandbox_pid = main.get_sandbox_pid()
    if not sandbox_pid:
        return 1, "", "Failed to find sandbox process"

    cmd = main.build_k8s_exec_command(sandbox_pid, command)

    try:
        # SECURITY: Pass explicit env to prevent leaking sidecar credentials
        # (Azure connection string, S3 keys, etc.) to agent code. Only the
        # fixed SAFE_EXEC_ENV, plus any already-resolved `extra_env` (never
        # caller-supplied literal values), is ever passed.
        merged_env = dict(main.SAFE_EXEC_ENV)
        if extra_env:
            merged_env.update(extra_env)
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=merged_env,
        )

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                asyncio.gather(
                    _read_stream_bounded(proc.stdout, main.EXEC_MAX_STDOUT_BYTES, proc),
                    _read_stream_bounded(proc.stderr, main.EXEC_MAX_STDERR_BYTES, proc),
                ),
                timeout=timeout
            )
            await proc.wait()
            return (
                proc.returncode or 0,
                stdout_bytes.decode('utf-8', errors='replace'),
                stderr_bytes.decode('utf-8', errors='replace'),
            )
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            return 124, "", f"Command timed out after {timeout}s"

    except Exception as e:
        logger.error(f"Exec failed: {e}")
        return 1, "", str(e)


@router.post("/exec", response_model=main.ExecResponse)
async def exec_command(req: main.ExecRequest):
    """
    Execute bash command in sandbox container.

    This is the only endpoint that needs process execution.
    Uses nsenter (K8s) or docker exec (Compose).

    `secret_env` (docs/SECRETS-DESIGN.md's bash_tool addendum): resolved
    the same way `_get_secret_value` resolves `{{secret:name}}` references
    for `/http-request` -- server-side, TTL-cached, gated by this session's
    own granted `secret_names`. Resolved values are merged into the
    exec'd process's environment (`exec_in_sandbox`'s `extra_env`), never
    templated into `req.command`, and scrubbed from stdout/stderr with the
    same exact-value `_scrub_secret_values` helper `/http-request` uses,
    before this response is ever built.
    """
    # Session exec budget (GitHub issue #122) -- independent of, and checked
    # before, this call's own `req.timeout`. Sticky once breached (by this
    # route or any other exec-like route): every exec-like call on this pod
    # is refused the same way until the next /configure.
    await main._reserve_session_exec_slot_or_raise(source="exec")

    import time as _time
    _t0 = _time.monotonic()
    logger.info(f"[exec] {req.command[:100]}...")

    resolved_secret_env: dict[str, str] = {}
    used_secrets: dict[str, str] = {}
    if req.secret_env:
        for env_var_name, secret_name in req.secret_env.items():
            if not main._validate_secret_env_var_name(env_var_name):
                logger.warning(
                    f"[exec] secret_env rejected an unsafe env var name "
                    f"{env_var_name!r}; silently omitted, not injected"
                )
                continue
            value = await main._get_secret_value(secret_name)
            if value is not None:
                resolved_secret_env[env_var_name] = value
                used_secrets[secret_name] = value

    exit_code, stdout, stderr = await main.exec_in_sandbox(
        req.command, req.timeout, extra_env=resolved_secret_env or None
    )

    if used_secrets:
        stdout = main._scrub_secret_values(stdout, used_secrets)
        stderr = main._scrub_secret_values(stderr, used_secrets)

    _t1 = _time.monotonic()
    duration_seconds = _t1 - _t0
    logger.info(f"[TIMING] exec: {(_t1 - _t0)*1000:.0f}ms (exit={exit_code}, cmd={req.command[:50]})")

    main._metrics_exec_count += 1
    if exit_code != 0:
        main._metrics_exec_errors += 1

    await main._record_session_exec_duration_or_raise(duration_seconds, source="exec")

    return main.ExecResponse(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr
    )


# ============================================================================
# Tool Call Proxy — Code Execution Mode
# ============================================================================
# Receives tool call requests from sandbox code (via _tool_bridge.py) and
# forwards them to the backend API for execution against real MCP tools.

@router.post("/tool-call")
async def tool_call_proxy(req: main.ToolCallRequest):
    """Proxy tool calls from sandbox code to the backend.

    Called by the sandbox-side tool bridge running inside the sandbox
    container. Forwards to the backend's internal tool-call endpoint which
    invokes the real MCP BaseTool instance.
    """
    import httpx

    backend_url = os.environ.get("BACKEND_URL", "http://host.docker.internal:8000")
    # SECURITY: always use this sidecar's own session, never req.session_id
    # (see the field comment on ToolCallRequest above).
    session_id = main.current_session.get("session_id")

    logger.info(f"[tool-call] {req.tool_name} (session={session_id})")

    try:
        async with httpx.AsyncClient(timeout=120.0) as client:
            resp = await client.post(
                f"{backend_url}/api/v1/internal/tool-call",
                json={
                    "tool_name": req.tool_name,
                    "arguments": req.arguments,
                    "session_id": session_id,
                },
            )
            if resp.status_code == 200:
                return resp.json()
            else:
                error_detail = resp.text[:500]
                logger.warning(f"[tool-call] Backend returned {resp.status_code}: {error_detail}")
                return {"error": f"Backend error ({resp.status_code}): {error_detail}"}
    except httpx.TimeoutException:
        logger.error(f"[tool-call] Timeout calling backend for {req.tool_name}")
        return {"error": f"Tool call timed out after 120s: {req.tool_name}"}
    except Exception as e:
        logger.error(f"[tool-call] Error: {e}")
        return {"error": str(e)}
