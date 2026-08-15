"""Scratch Memory Tool -- session-scoped agent bookkeeping that isn't the
workspace filesystem (GitHub issue #74).

Agents routinely need to track small working state across a session: a
running todo list, paths already reviewed, a plan outline. Doing that with
`file_create`/`file_str_replace` conflates two different things -- durable
workspace artifacts a human or downstream process cares about, versus
disposable agent bookkeeping nobody else should read -- and leaves
`.agent_notes.md`/`todo.json` litter behind in `file_glob` results and in
whatever the session's storage prefix gets flushed to.

This tool stores that state in the sidecar instead (see
sidecar/sidecar_scratch.py), where it never touches the workspace, never
syncs to storage, and is wiped when the pod is recycled for another tenant.

Deliberately minimal: opaque string values, four operations, no query
language. If an agent needs structure, it stores JSON as a string and
parses it itself -- this is a scratchpad, not a second database product.

Framework-agnostic: `create_scratch_memory_tool_spec()` returns a plain
`ToolSpec` (see ./types.py) whose handler is a normal async callable with
no agent-framework import anywhere in this file.
"""


import logging
from typing import Optional, TYPE_CHECKING

from ..lazy_runtime import resolve_sandbox_operation_context
from .types import ToolSpec

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

logger = logging.getLogger(__name__)

VALID_OPERATIONS = ("get", "set", "delete", "list")

SCRATCH_MEMORY_DESCRIPTION = """Store and retrieve your own working notes for this session, without writing files into the workspace.

Use this for bookkeeping that only you need: a running todo list, files
you've already reviewed, an outline of your plan, a hypothesis you want to
recheck later. Keeping it here instead of in a scratch file means the
workspace stays limited to real deliverables, and your notes don't show up
in file_glob/file_grep results or get saved with the project's output.

operation:
  "set"    - store `value` under `key` (replaces any existing value)
  "get"    - read one key back; tells you plainly if it isn't set yet
  "delete" - remove one key
  "list"   - list your keys and their sizes (not their contents)

Values are plain strings -- store JSON as a string if you need structure.
This memory lasts for this sandbox session only and is discarded when the
session ends; it is not a place to persist anything a user needs later.
Write those to the workspace with file_create instead.
"""

SCRATCH_MEMORY_PARAMETERS = {
    "type": "object",
    "properties": {
        "operation": {
            "type": "string",
            "enum": list(VALID_OPERATIONS),
            "description": "One of: get, set, delete, list",
        },
        "key": {
            "type": "string",
            "description": "Key to read/write/delete. Required for get, set, and delete; ignored for list.",
        },
        "value": {
            "type": "string",
            "description": "Value to store. Required for set; ignored otherwise.",
        },
    },
    "required": ["operation"],
}


def create_scratch_memory_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for scratch_memory.

    Args:
        sandbox_manager: SandboxManager instance (required, or lazy_runtime)
        session_id: Session ID for tracking
        lazy_runtime: Lazy sandbox runtime (required, or sandbox_manager)

    Returns:
        ToolSpec with a plain async handler(operation, key, value) -> str
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def scratch_memory(
        operation: str,
        key: Optional[str] = None,
        value: Optional[str] = None,
    ) -> str:
        if operation not in VALID_OPERATIONS:
            return (
                f"Error: unknown operation '{operation}'. "
                f"Expected one of: {', '.join(VALID_OPERATIONS)}"
            )
        # Checked here rather than left to the sidecar's 400: the model gets
        # a usable correction ("you forgot key") instead of an HTTP error
        # string it has to interpret.
        if operation in ("get", "set", "delete") and not key:
            return f"Error: 'key' is required for operation '{operation}'"
        if operation == "set" and value is None:
            return "Error: 'value' is required for operation 'set'"

        manager, effective_session_id = await resolve_sandbox_operation_context(
            lazy_runtime=lazy_runtime,
            sandbox_manager=sandbox_manager,
            session_id=session_id,
        )

        try:
            if operation == "set":
                result = await manager.scratch_set(effective_session_id, key, value)
                return f"Stored '{key}' ({result['bytes_stored']} bytes). Keys in memory: {result['keys']}"

            if operation == "get":
                result = await manager.scratch_get(effective_session_id, key)
                if not result.get("found"):
                    return f"No value stored for '{key}'."
                return result.get("value") or ""

            if operation == "delete":
                result = await manager.scratch_delete(effective_session_id, key)
                if not result.get("deleted"):
                    return f"No value stored for '{key}'; nothing to delete."
                return f"Deleted '{key}'. Keys in memory: {result['keys']}"

            result = await manager.scratch_list(effective_session_id)
            entries = result.get("entries", [])
            if not entries:
                return "Scratch memory is empty."
            listing = "\n".join(f"{e['key']} ({e['bytes']} bytes)" for e in entries)
            return (
                f"{result['keys']} key(s), {result['total_bytes']} bytes used "
                f"of {result['max_total_bytes']}:\n{listing}"
            )
        except Exception as e:
            logger.error(f"[scratch_memory] {operation} failed: {e}", exc_info=True)
            return f"Error running scratch_memory {operation}: {str(e)}"

    return ToolSpec(
        name="scratch_memory",
        description=SCRATCH_MEMORY_DESCRIPTION,
        parameters=SCRATCH_MEMORY_PARAMETERS,
        handler=scratch_memory,
    )
