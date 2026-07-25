"""
Search Tools - Read-only directory listing and content search in the sandbox

These tools proxy to already-implemented SandboxManager methods:
- ls: list direct children of a directory
- glob: find files by name pattern
- grep: search file contents by regex

Execution happens via SandboxManager HTTP calls to the sidecar container
(POST /ls, /glob, /grep — see sidecar/main.py). None of these tools mutate
the sandbox filesystem.

This module is framework-agnostic: `create_*_tool_spec()` factories return
plain `ToolSpec`s (see ./types.py) with no LangChain import anywhere in
this file. `create_*_tool()` are backward-compatible wrappers that adapt
the specs into LangChain tools.
"""

import logging
from typing import Optional, TYPE_CHECKING

from ..lazy_runtime import resolve_sandbox_operation_context
from .types import ToolSpec

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

logger = logging.getLogger(__name__)


def _format_entries(entries: list) -> str:
    if not entries:
        return "(empty)"
    lines = []
    for entry in entries:
        if isinstance(entry, dict):
            name = entry.get("path", "")
            marker = "/" if entry.get("is_dir") else ""
            size = entry.get("size")
            size_str = f"  ({size} bytes)" if size is not None and not entry.get("is_dir") else ""
            lines.append(f"{name}{marker}{size_str}")
        else:
            lines.append(str(entry))
    return "\n".join(lines)


LS_DESCRIPTION = """List the direct children of a directory in the sandbox workspace.

Use this before `view` on a directory you haven't explored yet, or
instead of `bash_tool("ls ...")` — same result, no shell round trip.
Only lists direct children; it is not recursive.

**Path format:**
- '/' - workspace root (default)
- 'reports' or '/workspace/reports' - a subdirectory
- '/mnt/user-data/uploads' - uploaded files

**Examples:**
- ls()  # List workspace root
- ls("reports")  # List a subdirectory
- ls("/mnt/user-data/uploads")  # List uploaded files

Args:
    path: Directory path to list (default "/")

Returns:
    Listing of direct child entries, or an error message
"""

LS_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": 'Directory path to list (default "/")',
            "default": "/",
        },
    },
    "required": [],
}


def create_ls_tool_spec(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for ls.

    Returns:
        ToolSpec with a plain async handler(path) -> str
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def ls(path: str = "/") -> str:
        path = (path or "/").strip() or "/"

        logger.info(f"[ls] Listing {path}")
        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            entries = await resolved_manager.ls(
                session_id=resolved_session_id,
                path=path,
            )
        except Exception as e:
            logger.error(f"[ls] Error: {e}", exc_info=True)
            return f"Error listing directory: {str(e)}"

        return f"Directory: {path}\n\n" + _format_entries(entries)

    return ToolSpec(
        name="ls",
        description=LS_DESCRIPTION,
        parameters=LS_PARAMETERS,
        handler=ls,
    )


def create_ls_tool(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
):
    """
    Create ls as a LangChain tool (backward-compatible wrapper).

    Prefer `create_ls_tool_spec()` for framework-agnostic use.
    Requires the `langchain` extra (`pip install boxkite-sandbox[langchain]`).

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_ls_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]


GLOB_DESCRIPTION = """Find files by name pattern in the sandbox workspace.

Use this to locate files by name or extension without knowing the
exact directory — e.g. "every Python file" or "every CSV under
reports/". Supports standard glob syntax including `**` for
recursive matches.

**Examples:**
- glob("**/*.py")  # Every Python file, recursively, from the root
- glob("*.csv", "reports")  # CSV files directly under reports/
- glob("**/*.md", "/workspace")  # Every Markdown file under /workspace

Args:
    pattern: Glob pattern to match file names against (e.g. "**/*.py")
    path: Directory to search under (default "/")

Returns:
    List of matching file paths, or an error message
"""

GLOB_PARAMETERS = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": 'Glob pattern to match file names against (e.g. "**/*.py")',
        },
        "path": {
            "type": "string",
            "description": 'Directory to search under (default "/")',
            "default": "/",
        },
    },
    "required": ["pattern"],
}


def create_glob_tool_spec(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for glob.

    Returns:
        ToolSpec with a plain async handler(pattern, path) -> str
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def glob(pattern: str, path: str = "/") -> str:
        if not pattern or not pattern.strip():
            return "Error: pattern is required"
        pattern = pattern.strip()
        path = (path or "/").strip() or "/"

        logger.info(f"[glob] Searching {path} for pattern {pattern!r}")
        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            matches = await resolved_manager.glob(
                session_id=resolved_session_id,
                pattern=pattern,
                path=path,
            )
        except Exception as e:
            logger.error(f"[glob] Error: {e}", exc_info=True)
            return f"Error searching for files: {str(e)}"

        if not matches:
            return f"No files matching {pattern!r} under {path}"

        return f"Matches for {pattern!r} under {path}:\n\n" + _format_entries(matches)

    return ToolSpec(
        name="glob",
        description=GLOB_DESCRIPTION,
        parameters=GLOB_PARAMETERS,
        handler=glob,
    )


def create_glob_tool(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
):
    """
    Create glob as a LangChain tool (backward-compatible wrapper).

    Prefer `create_glob_tool_spec()` for framework-agnostic use.
    Requires the `langchain` extra (`pip install boxkite-sandbox[langchain]`).

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_glob_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]


GREP_DESCRIPTION = """Search file contents by regex pattern in the sandbox workspace.

Use this to find where a symbol, string, or pattern appears across
files without viewing each one individually — e.g. "which files
import pandas" or "where is API_KEY referenced".

**Examples:**
- grep("def main\\\\(")  # Find function definitions matching a regex
- grep("TODO", "src")  # Search only under src/
- grep("import pandas", "/workspace", glob="*.py")  # Restrict to .py files

Args:
    pattern: Regex pattern to search for
    path: Directory to search under (default "/")
    glob: Optional glob to restrict which files are searched (e.g. "*.py")
    max_matches: Maximum number of matches to return (default 500)

Returns:
    Matching lines with file path and line number, or an error message
"""

GREP_PARAMETERS = {
    "type": "object",
    "properties": {
        "pattern": {
            "type": "string",
            "description": "Regex pattern to search for",
        },
        "path": {
            "type": "string",
            "description": 'Directory to search under (default "/")',
            "default": "/",
        },
        "glob": {
            "type": "string",
            "description": 'Optional glob to restrict which files are searched (e.g. "*.py")',
        },
        "max_matches": {
            "type": "integer",
            "description": "Maximum number of matches to return (default 500)",
            "default": 500,
        },
    },
    "required": ["pattern"],
}


def create_grep_tool_spec(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for grep.

    Returns:
        ToolSpec with a plain async handler(pattern, path, glob, max_matches) -> str
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def grep(
        pattern: str,
        path: str = "/",
        glob: Optional[str] = None,
        max_matches: int = 500,
    ) -> str:
        if not pattern or not pattern.strip():
            return "Error: pattern is required"
        pattern = pattern.strip()
        path = (path or "/").strip() or "/"

        logger.info(f"[grep] Searching {path} for pattern {pattern!r}")
        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            result = await resolved_manager.grep(
                session_id=resolved_session_id,
                pattern=pattern,
                path=path,
                glob=glob,
                max_matches=max_matches,
            )
        except Exception as e:
            logger.error(f"[grep] Error: {e}", exc_info=True)
            return f"Error searching file contents: {str(e)}"

        error = result.get("error")
        if error:
            return f"Error searching file contents: {error}"

        matches = result.get("matches", [])
        if not matches:
            return f"No matches for {pattern!r} under {path}"

        lines = []
        for match in matches:
            if isinstance(match, dict):
                match_path = match.get("path", "")
                line_no = match.get("line", "")
                text = match.get("text", "")
                lines.append(f"{match_path}:{line_no}: {text}")
            else:
                lines.append(str(match))

        header = f"Matches for {pattern!r} under {path}:\n\n"
        footer = "\n\n(truncated — refine the pattern or path for more)" if result.get("truncated") else ""
        return header + "\n".join(lines) + footer

    return ToolSpec(
        name="grep",
        description=GREP_DESCRIPTION,
        parameters=GREP_PARAMETERS,
        handler=grep,
    )


def create_grep_tool(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
):
    """
    Create grep as a LangChain tool (backward-compatible wrapper).

    Prefer `create_grep_tool_spec()` for framework-agnostic use.
    Requires the `langchain` extra (`pip install boxkite-sandbox[langchain]`).

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_grep_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]


WATCH_DIRECTORY_DESCRIPTION = """Wait for the first filesystem change under a directory in the sandbox workspace, or time out.

Use this instead of polling `ls` in a loop when you're waiting on a file another process will produce (a build's output, a test runner's report, a dev server writing its build cache).

Blocks for up to `timeout_seconds` (max 60), then returns either the first batch of changes it saw or an empty list with timed_out=true. Only reports changes that happen WHILE this call is running -- a change that already happened before you called this won't be reported; use `ls`/`glob` first if you need to check current state.
"""

WATCH_DIRECTORY_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Directory to watch (default: workspace root)",
            "default": "/",
        },
        "timeout_seconds": {
            "type": "number",
            "description": "How long to wait for a change, in seconds (default 10, max 60)",
            "default": 10,
        },
    },
    "required": [],
}


def create_watch_directory_tool_spec(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for watch_directory
    (docs/FILE-WATCHER-DESIGN.md).

    Returns:
        ToolSpec with a plain async handler(path, timeout_seconds) -> str
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def watch_directory(path: str = "/", timeout_seconds: float = 10.0) -> str:
        path = (path or "/").strip() or "/"

        logger.info(f"[watch_directory] Watching {path} for up to {timeout_seconds}s")
        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            result = await resolved_manager.watch_directory(
                session_id=resolved_session_id,
                path=path,
                timeout_seconds=timeout_seconds,
            )
        except Exception as e:
            logger.error(f"[watch_directory] Error: {e}", exc_info=True)
            return f"Error watching directory: {str(e)}"

        if result.get("timed_out"):
            return f"No changes under {path} within {timeout_seconds}s."

        changes = result.get("changes") or []
        if not changes:
            return f"No changes under {path} within {timeout_seconds}s."

        lines = [f"{c.get('event')}: {c.get('path')}" for c in changes]
        return f"Changes under {path}:\n" + "\n".join(lines)

    return ToolSpec(
        name="watch_directory",
        description=WATCH_DIRECTORY_DESCRIPTION,
        parameters=WATCH_DIRECTORY_PARAMETERS,
        handler=watch_directory,
    )


def create_watch_directory_tool(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
):
    """
    Create watch_directory as a LangChain tool (backward-compatible wrapper).

    Prefer `create_watch_directory_tool_spec()` for framework-agnostic use.
    Requires the `langchain` extra (`pip install boxkite-sandbox[langchain]`).

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_watch_directory_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]
