"""workspace_diff: what changed in the sandbox since you last looked (issue #71).

`watch_directory` answers "what is changing right now" and only sees changes
that occur while it blocks. This answers the question an agent actually has
after doing some work, including changes it did not make itself.
"""

import logging
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

from ..lazy_runtime import resolve_sandbox_operation_context
from .types import ToolSpec

logger = logging.getLogger(__name__)

WORKSPACE_DIFF_DESCRIPTION = """Show what changed in the sandbox workspace since the last time you called this.

Answers "what changed since I last looked" in one call, instead of re-deriving it with several ls/glob/view calls. Reports files you did not touch yourself -- output written by a build, a background process, a test run, or a previous turn.

Returns added, removed and modified paths, with a unified diff for modified text files. Build and VCS directories (.git, node_modules, __pycache__, .venv, dist, target, ...) are skipped by default.

The first call on a path has nothing to compare against: it establishes a baseline and reports no changes. Call it again after doing work to see the delta. Pass the `checkpoint` returned by an earlier call to compare against that specific point instead of the most recent one.

This is not `git diff` -- it sees every file, tracked or not, and does not care whether the workspace is a git repository at all.
"""

WORKSPACE_DIFF_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Directory to diff (default: workspace root)",
            "default": "/",
        },
        "checkpoint": {
            "type": "string",
            "description": (
                "Compare against this specific earlier checkpoint token. "
                "Omit to compare against the most recent snapshot of this path."
            ),
        },
        "exclude": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Extra directory names to skip, on top of the defaults",
        },
        "include_diffs": {
            "type": "boolean",
            "description": "Include unified diffs for modified text files (default true)",
            "default": True,
        },
    },
    "required": [],
}


def _format(result: dict, path: str) -> str:
    checkpoint = result.get("checkpoint", "")
    notes = result.get("notes") or []

    if result.get("baseline"):
        lines = [
            f"Baseline established for {path} ({result.get('files_scanned', 0)} files).",
            "No changes to report yet -- call workspace_diff again after making changes.",
            f"checkpoint: {checkpoint}",
        ]
        return "\n".join(lines + [f"note: {n}" for n in notes])

    changes = result.get("changes") or []
    if not changes:
        return f"No changes under {path} since the last check.\ncheckpoint: {checkpoint}"

    counts: dict[str, int] = {}
    for c in changes:
        counts[c.get("change", "?")] = counts.get(c.get("change", "?"), 0) + 1
    summary = ", ".join(f"{n} {k}" for k, n in sorted(counts.items()))

    lines = [f"Changes under {path} ({summary}):", ""]
    for c in changes:
        kind, cpath = c.get("change", "?"), c.get("path", "?")
        delta = c.get("size_delta_bytes", 0)
        sign = "+" if delta >= 0 else ""
        lines.append(f"{kind}: {cpath} ({sign}{delta} bytes)")
        if c.get("diff"):
            lines.append(c["diff"].rstrip("\n"))
        elif c.get("diff_omitted_reason"):
            lines.append(f"  (no diff: {c['diff_omitted_reason']})")
        lines.append("")

    # Caps are surfaced, never silent: a trimmed result that looks complete
    # would tell the agent nothing else changed, which is the wrong answer.
    if result.get("truncated"):
        lines.append("WARNING: scan was truncated; this list may be incomplete.")
    lines += [f"note: {n}" for n in notes]
    lines.append(f"checkpoint: {checkpoint}")
    return "\n".join(lines)


def create_workspace_diff_tool_spec(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for workspace_diff."""
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def workspace_diff(
        path: str = "/",
        checkpoint: Optional[str] = None,
        exclude: Optional[list] = None,
        include_diffs: bool = True,
    ) -> str:
        path = (path or "/").strip() or "/"

        logger.info(f"[workspace_diff] Diffing {path} (checkpoint={checkpoint})")
        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            result = await resolved_manager.workspace_diff(
                session_id=resolved_session_id,
                path=path,
                checkpoint=checkpoint,
                exclude=exclude,
                include_diffs=include_diffs,
            )
        except Exception as e:
            logger.error(f"[workspace_diff] Error: {e}", exc_info=True)
            return f"Error diffing workspace: {str(e)}"

        return _format(result or {}, path)

    return ToolSpec(
        name="workspace_diff",
        description=WORKSPACE_DIFF_DESCRIPTION,
        parameters=WORKSPACE_DIFF_PARAMETERS,
        handler=workspace_diff,
    )


def create_workspace_diff_tool(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
):
    """Create workspace_diff as a LangChain tool (backward-compatible wrapper).

    Prefer `create_workspace_diff_tool_spec()` for framework-agnostic use.
    Requires the `langchain` extra (`pip install boxxkite-sandbox[langchain]`).
    """
    from .adapters import to_langchain_tools

    spec = create_workspace_diff_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]
