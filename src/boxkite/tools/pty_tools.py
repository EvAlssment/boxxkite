"""
PTY Tools - agent-callable pseudo-terminal execution (docs/AGENT-PTY-DESIGN.md)

`bash_tool`'s exec path is a plain pipe -- fine for most commands, but a
program that checks `isatty()` or needs real terminal control codes
(curses menus, some installers, `htop`-style tools) needs an actual
pseudo-terminal. `pty_exec` runs ONE command behind a real PTY
(sidecar/main.py's `POST /pty-exec`, sharing the human-takeover `WS /pty`
route's `pty.openpty()`/nsenter plumbing, but execing the caller's argv
directly -- never an interactive shell) and returns its captured output,
bounded by a timeout, same request/response shape as every other tool.

Off by default at TWO layers, not one: `BOXKITE_AGENT_PTY_ENABLED` on the
sidecar itself (a 404 if unset) and this tool's own opt-in gate at the
factory layer (`enable_agent_pty` in `create_sandbox_tool_specs`) -- this
is new attack surface (a second, agent-reachable PTY-allocation path), not
a variant of an already-reviewed feature, so it gets the same
defense-in-depth double-gate the declarative builder's
BOXKITE_IMAGE_BUILDER_ENABLED + explicit account opt-in already has.

This module is framework-agnostic: `create_pty_exec_tool_spec()` returns a
plain `ToolSpec` (see ./types.py) with no LangChain import anywhere in
this file.
"""

import logging
import time
from typing import Optional, TYPE_CHECKING
from uuid import UUID

from ..audit import AuditSink, safe_call
from ..command_whitelist import validate_command_whitelist
from ..lazy_runtime import resolve_sandbox_operation_context
from .types import ToolSpec

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

logger = logging.getLogger(__name__)

PTY_EXEC_DESCRIPTION = """Run ONE command behind a real pseudo-terminal (PTY), for programs that need one.

Use this instead of bash_tool ONLY when a command specifically needs a real terminal -- checks isatty(), uses curses/raw terminal mode, or refuses to run non-interactively. For everything else, prefer bash_tool.

Not an interactive shell -- one command per call, bounded by timeout_seconds. Optionally write input_bytes to answer an interactive prompt. May not be available (404) if this deployment hasn't enabled it.
"""

PTY_EXEC_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "Command to run (split like a shell would, but never actually run through a shell -- no ;, &&, or $() interpretation)",
        },
        "input_text": {
            "type": "string",
            "description": "Optional text to write to the command's stdin (e.g. answering a prompt)",
            "default": "",
        },
        "timeout_seconds": {
            "type": "number",
            "description": "How long to wait before killing the process (default 30, max 120)",
            "default": 30,
        },
    },
    "required": ["command"],
}


def create_pty_exec_tool_spec(
    session_id: Optional[str] = None,
    sandbox_manager: Optional['SandboxManager'] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
    allowed_commands: Optional[list] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for pty_exec.

    Args:
        allowed_commands: Optional per-agent command allowlist, same
            enforcement `bash_tool` already applies
            (command_whitelist.validate_command_whitelist) -- per the #69
            security review, pty_exec previously bypassed this entirely,
            which would have let an agent run commands outside its
            configured allowlist purely by switching tools. Now enforced
            identically to bash_tool, before a PTY is ever allocated.
        organization_id: Organization ID (for audit trail)
        work_item_id: Work item ID (for audit trail)
        agent_name: Optional agent name for audit trail
        audit_sink: Optional AuditSink to mirror executed commands into an
            external system -- per the #69 review, pty_exec previously
            wrote no audit record at all (not mislabeled, just missing),
            unlike bash_tool's record_exec call on every command. Now
            mirrored the same way, so pty_exec calls are attributable in
            the same audit trail bash_tool/http_request already use.

    Returns:
        ToolSpec with a plain async handler(command, input_text, timeout_seconds) -> str
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    canonical_session_id = str(session_id).strip() if session_id else None
    session_id_uuid: Optional[UUID] = None
    if canonical_session_id:
        bare_id = (
            canonical_session_id.split(":", 1)[1]
            if ":" in canonical_session_id
            else canonical_session_id
        )
        try:
            session_id_uuid = UUID(bare_id)
        except ValueError:
            session_id_uuid = None

    async def pty_exec(command: str, input_text: str = "", timeout_seconds: float = 30.0) -> str:
        if not command or not command.strip():
            return "Error: Empty command provided"

        # SECURITY: same per-agent command whitelist bash_tool enforces --
        # a PTY is a different execution mechanism, not a different
        # authorization boundary; an agent should never be able to reach a
        # command its allowlist excludes just by calling pty_exec instead
        # of bash_tool.
        if allowed_commands:
            is_allowed, whitelist_error = validate_command_whitelist(command, allowed_commands)
            if not is_allowed:
                logger.warning(f"[pty_exec] Whitelist blocked command: {command[:100]}")
                return whitelist_error

        logger.info(f"[pty_exec] Running: {command[:200]}...")
        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            start_time = time.monotonic()
            result = await resolved_manager.pty_exec(
                session_id=resolved_session_id,
                command=command,
                input_bytes=input_text.encode() if input_text else b"",
                timeout_seconds=timeout_seconds,
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)
        except Exception as e:
            logger.error(f"[pty_exec] Error: {e}", exc_info=True)
            return f"Error running command in PTY: {str(e)}"

        output = result.get("output", "")
        exit_code = result.get("exit_code")
        timed_out = result.get("timed_out")

        if audit_sink:
            await safe_call(
                audit_sink,
                "record_exec",
                organization_id=organization_id,
                work_item_id=work_item_id,
                session_id=str(session_id_uuid) if session_id_uuid else canonical_session_id,
                agent_name=agent_name,
                command=command,
                exit_code=exit_code if exit_code is not None else -1,
                duration_ms=duration_ms,
            )

        if timed_out:
            return f"Command timed out after {timeout_seconds}s. Output so far:\n{output}"

        return f"(exit code {exit_code})\n{output}" if output else f"(exit code {exit_code}, no output)"

    return ToolSpec(
        name="pty_exec",
        description=PTY_EXEC_DESCRIPTION,
        parameters=PTY_EXEC_PARAMETERS,
        handler=pty_exec,
    )


def create_pty_exec_tool(
    session_id: Optional[str] = None,
    sandbox_manager: Optional['SandboxManager'] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
    allowed_commands: Optional[list] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
):
    """
    Create pty_exec as a LangChain tool (backward-compatible wrapper).

    Prefer `create_pty_exec_tool_spec()` for framework-agnostic use.
    Requires the `langchain` extra (`pip install boxkite-sandbox[langchain]`).

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_pty_exec_tool_spec(
        session_id=session_id,
        sandbox_manager=sandbox_manager,
        lazy_runtime=lazy_runtime,
        allowed_commands=allowed_commands,
        organization_id=organization_id,
        work_item_id=work_item_id,
        agent_name=agent_name,
        audit_sink=audit_sink,
    )
    return to_langchain_tools([spec])[0]
