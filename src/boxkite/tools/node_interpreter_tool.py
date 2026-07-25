"""
Node Interpreter Tool - Execute JavaScript in a persistent, kept-alive Node.js
process

The Node.js counterpart to python_interpreter_tool.py -- see that module's
own docstring for the general shape this mirrors: distinct from bash_tool
(which always runs `node -e ...`/`node script.js` as a fresh subprocess per
call, so variables never survive between tool calls), this tool executes
each snippet against one Node.js process the sidecar keeps alive for the
whole session -- state assigned in one call (via `var`, bare assignment, or
top-level `let`/`const`/`class`/`function`) is visible to later calls, until
the interpreter is reset, times out from inactivity, or the session itself
is torn down or recycled.

See docs/NODE-INTERPRETER-DESIGN.md and sidecar/main.py's
/node-interpreter/* endpoints.

Opt-in at TWO layers, not one, matching pty_tools.py's convention for new
attack surface: this tool is only wired into create_sandbox_tool_specs()
when the caller passes enable_node_interpreter=True, AND the sidecar itself
404s every /node-interpreter/* call unless BOXKITE_NODE_INTERPRETER_ENABLED
is set server-side -- this Python-layer flag alone does not turn the route
on.

SECURITY:
- Runs inside the same sandbox container, under the same UID, with the same
  network isolation as bash_tool's /exec calls -- see exec_in_sandbox and
  _spawn_node_interpreter in sidecar/main.py.
- Output is sanitized with the same secret-shaped-string redaction bash_tool
  uses (see sanitize_output in bash_tool.py).

This module is framework-agnostic: `create_node_interpreter_tool_spec()`
returns a plain `ToolSpec` (see ./types.py) whose handler is a normal async
callable with no LangChain import anywhere in this file.
`create_node_interpreter_tool()` is a backward-compatible wrapper that
adapts that spec into a LangChain tool (see ./adapters.py) for existing
callers.
"""

import logging
import time
from typing import Optional, TYPE_CHECKING
from uuid import UUID

from ..audit import AuditSink, safe_call
from ..lazy_runtime import resolve_sandbox_operation_context
from .bash_tool import sanitize_output
from .types import ToolSpec

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

logger = logging.getLogger(__name__)

NODE_INTERPRETER_DESCRIPTION = """
Execute JavaScript (Node.js) code against a persistent, kept-alive
interpreter.

Unlike bash_tool's `node -e ...` (a fresh process every call), variables
and top-level declarations (`var`, `let`, `const`, `function`, `class`)
from earlier node_interpreter calls are still available here -- use this
when a task needs to build up state across multiple calls (e.g. load and
parse a JSON file once, then run several separate queries against it).

Re-declaring the same `let`/`const`/`class` name across two separate calls
raises a SyntaxError ("Identifier '<name>' has already been declared") --
this is genuine JavaScript top-level-scope behavior (the same thing happens
typing the same `let` twice into a real Node REPL or a browser devtools
console), not a bug. Reassign without the keyword, or use a new name.

The interpreter is reset automatically after a period of inactivity, or if
the process is killed by its own memory limit -- if you get an unexpected
ReferenceError for a variable you set earlier, the interpreter has likely
been reset; just re-run your setup code.

Returns stdout produced by the snippet (console.log/error/warn/info), plus
util.inspect() of the last expression's value (if any), the same way a
REPL/devtools console echoes it back.

Args:
    code: JavaScript code to execute
    timeout: Timeout in seconds (default 30, max 300)

Returns:
    stdout, followed by the last expression's inspected value (if any), or
    an error stack trace if the snippet threw
"""

NODE_INTERPRETER_PARAMETERS = {
    "type": "object",
    "properties": {
        "code": {
            "type": "string",
            "description": "JavaScript code to execute",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds (default 30, max 300)",
            "default": 30,
        },
    },
    "required": ["code"],
}


def create_node_interpreter_tool_spec(
    session_id: Optional[str] = None,
    sandbox_manager: Optional["SandboxManager"] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for node_interpreter.

    Mirrors create_python_interpreter_tool_spec's shape exactly -- see its
    docstring for the AuditSink integration details, which are identical
    here.

    Args:
        session_id: Session ID for tracking
        sandbox_manager: SandboxManager instance (required unless lazy_runtime is provided)
        lazy_runtime: Optional LazySandboxRuntime shared across agent/subagents
        organization_id: Organization ID (for audit trail)
        work_item_id: Work item ID (for audit trail)
        agent_name: Optional agent name for audit trail
        audit_sink: Optional AuditSink to mirror executed snippets into an
            external system

    Returns:
        ToolSpec with a plain async handler(code, timeout) -> str
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
            logger.warning(
                f"[node_interpreter_tool] Non-UUID session_id={canonical_session_id!r}; "
                "AuditSink session linkage will be omitted where UUID is required"
            )
            session_id_uuid = None

    async def node_interpreter(code: str, timeout: int = 30) -> str:
        if not code or not code.strip():
            return "Error: Empty code provided"

        effective_timeout = min(max(1, timeout), 300)
        logger.info(f"[node_interpreter] Executing: {code[:200]}...")

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            start_time = time.monotonic()
            result = await resolved_manager.node_interpreter_exec(
                session_id=resolved_session_id,
                code=code,
                timeout=effective_timeout,
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)

            stdout = sanitize_output(result.get("stdout", ""))
            repr_result = result.get("result")
            error = result.get("error")
            truncated = result.get("truncated", False)

            if audit_sink:
                await safe_call(
                    audit_sink,
                    "record_exec",
                    organization_id=organization_id,
                    work_item_id=work_item_id,
                    session_id=str(session_id_uuid) if session_id_uuid else canonical_session_id,
                    agent_name=agent_name,
                    command=code,
                    exit_code=1 if error else 0,
                    duration_ms=duration_ms,
                )

            if error:
                error = sanitize_output(error)
                output = f"{stdout}{error}" if stdout else error
                return f"Error:\n{output}"

            parts = []
            if stdout:
                parts.append(stdout)
            if repr_result is not None:
                parts.append(sanitize_output(repr_result))
            output = "\n".join(parts) if parts else "(no output)"
            if truncated:
                output += "\n[output truncated]"
            return output

        except Exception as e:
            logger.error(f"[node_interpreter] Execution error: {e}", exc_info=True)
            return f"Error executing code: {str(e)}"

    return ToolSpec(
        name="node_interpreter",
        description=NODE_INTERPRETER_DESCRIPTION,
        parameters=NODE_INTERPRETER_PARAMETERS,
        handler=node_interpreter,
    )


def create_node_interpreter_tool(
    session_id: Optional[str] = None,
    sandbox_manager: Optional["SandboxManager"] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
):
    """
    Create the node_interpreter tool as a LangChain tool (backward-compatible wrapper).

    Prefer `create_node_interpreter_tool_spec()` for framework-agnostic use
    -- this function just adapts that spec via
    boxkite.tools.adapters.to_langchain_tools, kept for existing callers
    that expect a LangChain BaseTool directly. Requires the `langchain`
    extra (`pip install boxkite-sandbox[langchain]`).

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_node_interpreter_tool_spec(
        session_id=session_id,
        sandbox_manager=sandbox_manager,
        lazy_runtime=lazy_runtime,
        organization_id=organization_id,
        work_item_id=work_item_id,
        agent_name=agent_name,
        audit_sink=audit_sink,
    )
    return to_langchain_tools([spec])[0]
