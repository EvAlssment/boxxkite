"""Process Tools — start, poll, feed input to, stop, and list background
processes in the sandbox.

Distinct from bash_tool/`/exec`: bash_tool is strictly one-shot request/
response, bounded by its own timeout. These tools proxy to
SandboxManager.start_process/get_process_output/send_process_input/
stop_process/list_processes, which track a process across multiple tool
calls via the sidecar's `/process/*` registry (sidecar/main.py) instead of
awaiting it to completion in a single HTTP call. See
docs/PROCESS-SESSIONS-DESIGN.md.

Non-goal, stated explicitly rather than silently: a backgrounded process's
listening sockets are not reachable from any other tool call (per-exec
network isolation applies the same way it does to bash_tool) -- these tools
let an agent start something, watch its output, feed it input, and stop it,
not expose it on the network.

This module is framework-agnostic: `create_*_tool_spec()` functions each
return a plain `ToolSpec` (see ./types.py) whose handler is a normal async
callable with no LangChain import anywhere in this file. `create_*_tool()`
functions are backward-compatible wrappers that adapt those specs into
LangChain tools (see ./adapters.py) for existing callers.
"""

import logging
from typing import Optional, TYPE_CHECKING

from ..command_whitelist import validate_command_whitelist
from ..lazy_runtime import resolve_sandbox_operation_context
from .types import ToolSpec

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

logger = logging.getLogger(__name__)

# Mirrors sidecar/main.py's PROCESS_MAX_RUNTIME_SECONDS_CEILING default --
# kept in sync manually since the tool layer and sidecar are deployed
# separately; the sidecar's own ceiling is the actual enforcement point,
# this is only a sane client-side default.
DEFAULT_MAX_RUNTIME_SECONDS = 3600


def _format_process_list(processes: list) -> str:
    if not processes:
        return "(no background processes)"
    lines = []
    for proc in processes:
        if not isinstance(proc, dict):
            lines.append(str(proc))
            continue
        description = f" ({proc.get('description')})" if proc.get("description") else ""
        exit_code = proc.get("exit_code")
        exit_str = f", exit_code={exit_code}" if exit_code is not None else ""
        lines.append(
            f"{proc.get('process_id')}{description}: {proc.get('status')}{exit_str} "
            f"-- {proc.get('command')}"
        )
    return "\n".join(lines)


START_PROCESS_DESCRIPTION = """
Start a long-running background process in the sandbox (a dev
server, a test watcher, a long build, a REPL) that keeps running
after this tool call returns.

Use this instead of bash_tool when a command should keep running
past a single tool call -- bash_tool is one-shot and bounded by its
own timeout, so it cannot be used to "start something and keep
going." Poll its output with get_process_output, feed it input with
send_process_input, and stop it with stop_process when done.

**Non-goal:** a process started here is not reachable over the
network from any other tool call (the same per-command network
isolation bash_tool has also applies here) -- this is for watching
output and feeding input, not for exposing a service.

Args:
    command: Shell command to run in the background
    description: Optional human-readable label for this process
    max_runtime_seconds: Hard ceiling on how long the process may
        run before it is force-killed (default 3600)

Returns:
    The process_id to use with get_process_output/send_process_input/
    stop_process, or an error message
"""

START_PROCESS_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "Shell command to run in the background",
        },
        "description": {
            "type": "string",
            "description": "Optional human-readable label for this process",
        },
        "max_runtime_seconds": {
            "type": "integer",
            "description": "Hard ceiling on how long the process may run before it is force-killed (default 3600)",
            "default": DEFAULT_MAX_RUNTIME_SECONDS,
        },
    },
    "required": ["command"],
}


def create_start_process_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    allowed_commands: Optional[list] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for start_process.

    Args:
        sandbox_manager: SandboxManager instance (required, or lazy_runtime)
        session_id: Session ID for tracking
        lazy_runtime: Lazy sandbox runtime (required, or sandbox_manager)
        allowed_commands: Optional per-agent command allowlist. When set
            (non-empty), only these program names may appear in command
            positions -- see command_whitelist.validate_command_whitelist.
            This must mirror bash_tool's enforcement so agents cannot bypass
            the whitelist by starting a background process instead.

    Returns:
        ToolSpec with a plain async handler(command, description,
        max_runtime_seconds) -> str
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def start_process(
        command: str,
        description: Optional[str] = None,
        max_runtime_seconds: int = DEFAULT_MAX_RUNTIME_SECONDS,
    ) -> str:
        if not command or not command.strip():
            return "Error: Empty command provided"

        # SECURITY: Per-agent command whitelist (when configured, only the
        # allowed program names may appear in any command position). This
        # mirrors bash_tool's enforcement so this tool cannot be used to
        # bypass an agent's command restriction.
        if allowed_commands:
            is_allowed, whitelist_error = validate_command_whitelist(command, allowed_commands)
            if not is_allowed:
                logger.warning(f"[start_process] Whitelist blocked command: {command[:100]}")
                return whitelist_error

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            result = await resolved_manager.start_process(
                session_id=resolved_session_id,
                command=command,
                description=description,
                max_runtime_seconds=max_runtime_seconds,
            )
        except Exception as e:
            logger.error(f"[start_process] Error: {e}", exc_info=True)
            return f"Error starting process: {str(e)}"

        return (
            f"Started process {result.get('process_id')} (status={result.get('status')}). "
            f"Use get_process_output(\"{result.get('process_id')}\") to check on it."
        )

    return ToolSpec(
        name="start_process",
        description=START_PROCESS_DESCRIPTION,
        parameters=START_PROCESS_PARAMETERS,
        handler=start_process,
    )


def create_start_process_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    allowed_commands: Optional[list] = None,
):
    """Create the start_process tool as a LangChain tool (backward-compatible wrapper).

    Prefer `create_start_process_tool_spec()` for framework-agnostic use.
    Requires the `langchain` extra.

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_start_process_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
        allowed_commands=allowed_commands,
    )
    return to_langchain_tools([spec])[0]


GET_PROCESS_OUTPUT_DESCRIPTION = """
Poll a background process's output since a given byte offset.

Call this repeatedly to watch a process's progress. `since_offset`
(from a previous call's response, or 0 the first time) lets you
fetch only the new output since your last check. A response of
`truncated: true` means the process has produced more output than
the sidecar buffers, and the earliest bytes are gone -- not a bug.

Args:
    process_id: The process_id returned by start_process
    since_offset: Byte offset to read from (default 0, meaning
        everything currently buffered)

Returns:
    The process's status, new output, next offset to poll from, and
    exit code (once it has one), or an error message
"""

GET_PROCESS_OUTPUT_PARAMETERS = {
    "type": "object",
    "properties": {
        "process_id": {
            "type": "string",
            "description": "The process_id returned by start_process",
        },
        "since_offset": {
            "type": "integer",
            "description": "Byte offset to read from (default 0, meaning everything currently buffered)",
            "default": 0,
        },
    },
    "required": ["process_id"],
}


def create_get_process_output_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for get_process_output."""
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def get_process_output(process_id: str, since_offset: int = 0) -> str:
        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            result = await resolved_manager.get_process_output(
                session_id=resolved_session_id,
                process_id=process_id,
                since_offset=since_offset,
            )
        except Exception as e:
            logger.error(f"[get_process_output] Error: {e}", exc_info=True)
            return f"Error getting process output: {str(e)}"

        lines = [f"status: {result.get('status')}"]
        if result.get("exit_code") is not None:
            lines.append(f"exit_code: {result.get('exit_code')}")
        if result.get("truncated"):
            lines.append("(earlier output was truncated -- the buffer only keeps recent output)")
        lines.append(f"next_offset: {result.get('next_offset')}")
        stdout_chunk = result.get("stdout_chunk") or ""
        lines.append("--- output ---")
        lines.append(stdout_chunk if stdout_chunk else "(no new output)")

        return "\n".join(lines)

    return ToolSpec(
        name="get_process_output",
        description=GET_PROCESS_OUTPUT_DESCRIPTION,
        parameters=GET_PROCESS_OUTPUT_PARAMETERS,
        handler=get_process_output,
    )


def create_get_process_output_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
):
    """Create the get_process_output tool as a LangChain tool (backward-compatible wrapper)."""
    from .adapters import to_langchain_tools

    spec = create_get_process_output_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]


SEND_PROCESS_INPUT_DESCRIPTION = """
Write to a background process's stdin (e.g. answering an
interactive prompt in a REPL).

Args:
    process_id: The process_id returned by start_process
    data: Text to write to the process's stdin (include a trailing
        "\\n" if the process reads line by line)

Returns:
    Confirmation of bytes written, or an error message
"""

SEND_PROCESS_INPUT_PARAMETERS = {
    "type": "object",
    "properties": {
        "process_id": {
            "type": "string",
            "description": "The process_id returned by start_process",
        },
        "data": {
            "type": "string",
            "description": 'Text to write to the process\'s stdin (include a trailing "\\n" if the process reads line by line)',
        },
    },
    "required": ["process_id", "data"],
}


def create_send_process_input_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for send_process_input."""
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def send_process_input(process_id: str, data: str) -> str:
        if not data:
            return "Error: data is required"

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            result = await resolved_manager.send_process_input(
                session_id=resolved_session_id,
                process_id=process_id,
                data=data,
            )
        except Exception as e:
            logger.error(f"[send_process_input] Error: {e}", exc_info=True)
            return f"Error sending input to process: {str(e)}"

        return f"Wrote {result.get('bytes_written')} bytes to process {process_id}"

    return ToolSpec(
        name="send_process_input",
        description=SEND_PROCESS_INPUT_DESCRIPTION,
        parameters=SEND_PROCESS_INPUT_PARAMETERS,
        handler=send_process_input,
    )


def create_send_process_input_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
):
    """Create the send_process_input tool as a LangChain tool (backward-compatible wrapper)."""
    from .adapters import to_langchain_tools

    spec = create_send_process_input_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]


STOP_PROCESS_DESCRIPTION = """
Stop a background process (SIGTERM, then SIGKILL if it doesn't
exit on its own within a few seconds).

Args:
    process_id: The process_id returned by start_process

Returns:
    The process's final status and exit code, or an error message
"""

STOP_PROCESS_PARAMETERS = {
    "type": "object",
    "properties": {
        "process_id": {
            "type": "string",
            "description": "The process_id returned by start_process",
        },
    },
    "required": ["process_id"],
}


def create_stop_process_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for stop_process."""
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def stop_process(process_id: str) -> str:
        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            result = await resolved_manager.stop_process(
                session_id=resolved_session_id,
                process_id=process_id,
            )
        except Exception as e:
            logger.error(f"[stop_process] Error: {e}", exc_info=True)
            return f"Error stopping process: {str(e)}"

        return f"Process {process_id}: {result.get('status')} (exit_code={result.get('exit_code')})"

    return ToolSpec(
        name="stop_process",
        description=STOP_PROCESS_DESCRIPTION,
        parameters=STOP_PROCESS_PARAMETERS,
        handler=stop_process,
    )


def create_stop_process_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
):
    """Create the stop_process tool as a LangChain tool (backward-compatible wrapper)."""
    from .adapters import to_langchain_tools

    spec = create_stop_process_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]


LIST_PROCESSES_DESCRIPTION = """
List every background process currently tracked in this sandbox
session (running, exited, or stopped).

Use this to check what's still running before starting another
background process, or to find a process_id you've lost track of.

Returns:
    A listing of tracked processes with their status, or an error
    message
"""

LIST_PROCESSES_PARAMETERS = {
    "type": "object",
    "properties": {},
    "required": [],
}


def create_list_processes_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for list_processes."""
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def list_processes() -> str:
        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            processes = await resolved_manager.list_processes(session_id=resolved_session_id)
        except Exception as e:
            logger.error(f"[list_processes] Error: {e}", exc_info=True)
            return f"Error listing processes: {str(e)}"

        return _format_process_list(processes)

    return ToolSpec(
        name="list_processes",
        description=LIST_PROCESSES_DESCRIPTION,
        parameters=LIST_PROCESSES_PARAMETERS,
        handler=list_processes,
    )


def create_list_processes_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
):
    """Create the list_processes tool as a LangChain tool (backward-compatible wrapper)."""
    from .adapters import to_langchain_tools

    spec = create_list_processes_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]
