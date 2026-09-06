"""explain_last_failure: the last failed command plus what it touched (issue #77).

Collapses the mechanical part of the failure-diagnosis loop -- what ran, what
it printed, what it wrote -- into one call.
"""

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

from ..lazy_runtime import resolve_sandbox_operation_context
from .types import ToolSpec

logger = logging.getLogger(__name__)

EXPLAIN_LAST_FAILURE_DESCRIPTION = """Get the most recent failed command in this sandbox, together with the files it touched while running.

Use this right after a command exits nonzero, instead of re-running it with more verbosity or hunting for the files it was supposed to produce. Returns the command, its exit code, its stdout and stderr, how long it ran, and every file whose modification time falls inside its runtime window.

Only tracks commands run through bash_tool. Background processes and the PTY are not recorded.

Deleted files are not reported: this works by reading modification times, and a deleted file has none. Use workspace_diff when you need deletions.
"""

EXPLAIN_LAST_FAILURE_PARAMETERS = {
    "type": "object",
    "properties": {
        "include_touched_files": {
            "type": "boolean",
            "description": (
                "Include the files the command touched (default true). "
                "Set false to skip the filesystem scan when you only want the output."
            ),
            "default": True,
        },
    },
    "required": [],
}


def _format(result: dict) -> str:
    if not result.get("found"):
        notes = result.get("notes") or ["No command has failed in this session yet."]
        return "\n".join(notes)

    lines = [
        f"Last failed command (exit {result.get('exit_code')}, "
        f"{result.get('duration_seconds', 0):.2f}s):",
        f"  {result.get('command', '')}",
        "",
    ]

    stdout = result.get("stdout") or ""
    stderr = result.get("stderr") or ""
    lines.append("stderr:")
    lines.append(stderr.rstrip("\n") if stderr.strip() else "  (empty)")
    lines.append("")
    lines.append("stdout:")
    lines.append(stdout.rstrip("\n") if stdout.strip() else "  (empty)")
    lines.append("")

    touched = result.get("touched_files") or []
    if touched:
        lines.append(f"Files touched while it ran ({len(touched)}):")
        lines += [f"  {t.get('path')} ({t.get('size_bytes', 0)} bytes)" for t in touched]
    else:
        lines.append("Files touched while it ran: none detected.")
    lines.append("")

    # The deletions caveat and every cap ride along in notes. A tool that
    # answers "why did this fail" must not let a partial answer look total.
    lines += [f"note: {n}" for n in (result.get("notes") or [])]
    return "\n".join(lines)


def create_explain_last_failure_tool_spec(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for explain_last_failure."""
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def explain_last_failure(include_touched_files: bool = True) -> str:
        logger.info("[explain_last_failure] Fetching last failure")
        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            result = await resolved_manager.explain_last_failure(
                session_id=resolved_session_id,
                include_touched_files=include_touched_files,
            )
        except Exception as e:
            logger.error(f"[explain_last_failure] Error: {e}", exc_info=True)
            return f"Error fetching last failure: {str(e)}"

        return _format(result or {})

    return ToolSpec(
        name="explain_last_failure",
        description=EXPLAIN_LAST_FAILURE_DESCRIPTION,
        parameters=EXPLAIN_LAST_FAILURE_PARAMETERS,
        handler=explain_last_failure,
    )


def create_explain_last_failure_tool(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
):
    """Create explain_last_failure as a LangChain tool (backward-compatible wrapper).

    Prefer `create_explain_last_failure_tool_spec()` for framework-agnostic use.
    Requires the `langchain` extra (`pip install boxxkite-sandbox[langchain]`).
    """
    from .adapters import to_langchain_tools

    spec = create_explain_last_failure_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]
