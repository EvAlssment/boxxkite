"""
LSP Tools - agent-invokable code completions via persistent language
servers (docs/LSP-SUPPORT-SCOPING.md, GitHub issue #183).

Three narrow primitives against the sidecar's `/lsp/*` routes
(`sidecar/sidecar_lsp.py`): `lsp_start` spawns a persistent language server
(pyright for Python, typescript-language-server for TypeScript/JS) and runs
its initialize handshake; `lsp_completion` opens/updates the target file on
that server (full-document sync -- always the current on-disk content, no
incremental diff) and requests `textDocument/completion` at a position;
`lsp_stop` gracefully shuts the server down. Deliberately narrower than
issue #81's full "LSP support": completion only, no hover/signatureHelp/
diagnostics push -- see the scoping doc for the explicit deferred list.

Off by default at TWO layers, not one, matching pty_tools.py's/
node_interpreter_tool.py's convention for new attack surface: these tools
are only wired into create_sandbox_tool_specs() when the caller passes
enable_lsp_tools=True, AND the sidecar itself 404s every /lsp/* call unless
BOXKITE_LSP_ENABLED is set server-side -- this Python-layer flag alone does
not turn the routes on.

This module is framework-agnostic: `create_lsp_tool_specs()` returns plain
`ToolSpec`s (see ./types.py) whose handlers are normal async callables with
no LangChain import anywhere in this file.
"""

import logging
import time
from typing import Optional, TYPE_CHECKING
from uuid import UUID

from ..audit import AuditSink, safe_call
from ..lazy_runtime import resolve_sandbox_operation_context
from .types import ToolSpec

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

logger = logging.getLogger(__name__)

_SUPPORTED_LANGUAGES = ("python", "typescript")

# LSP CompletionItemKind enum (numeric codes the protocol actually sends) --
# translated to a short, human-readable string for the agent-facing output.
# Unknown codes fall back to "unknown" rather than raising: LSP responses
# are permissive by spec, a future server version could add a kind this map
# doesn't know about yet.
_COMPLETION_ITEM_KIND_NAMES = {
    1: "text", 2: "method", 3: "function", 4: "constructor", 5: "field",
    6: "variable", 7: "class", 8: "interface", 9: "module", 10: "property",
    11: "unit", 12: "value", 13: "enum", 14: "keyword", 15: "snippet",
    16: "color", 17: "file", 18: "reference", 19: "folder", 20: "enum_member",
    21: "constant", 22: "struct", 23: "event", 24: "operator", 25: "type_parameter",
}


def _simplify_completion_items(raw_items: list) -> list[dict]:
    """Translate raw LSP CompletionItem payloads into the small,
    agent-readable shape this tool returns (label/kind/detail/insertText).

    Every field has an explicit fallback -- LSP's CompletionItem has
    exactly one required field (`label`); `kind`, `detail`, and
    `insertText` are all optional per spec, and a real server response can
    (and does) omit any of them.
    """
    simplified = []
    for raw in raw_items:
        if not isinstance(raw, dict):
            continue
        label = raw.get("label", "")
        kind_name = _COMPLETION_ITEM_KIND_NAMES.get(raw.get("kind"), "unknown")
        detail = raw.get("detail")
        # Per the LSP spec: when insertText is absent, label is what a
        # client should insert.
        insert_text = raw.get("insertText") or label
        simplified.append(
            {"label": label, "kind": kind_name, "detail": detail, "insertText": insert_text}
        )
    return simplified


LSP_START_DESCRIPTION = """
Start a persistent language server for code intelligence (completions only).

Supported languages: "python" (pyright) and "typescript" (typescript-language-server, also covers JavaScript).

Returns an opaque lsp_id -- pass it to lsp_completion/lsp_stop for this same server. Starting a new server for a language you already started one for is wasteful; reuse the existing lsp_id instead. May not be available (error) if this deployment hasn't enabled LSP support.

Args:
    language: "python" or "typescript"

Returns:
    A message containing the lsp_id to use in subsequent calls
"""

LSP_START_PARAMETERS = {
    "type": "object",
    "properties": {
        "language": {
            "type": "string",
            "enum": list(_SUPPORTED_LANGUAGES),
            "description": "Language server to start: python or typescript",
        },
    },
    "required": ["language"],
}


LSP_COMPLETION_DESCRIPTION = """
Get code completions at a position in a file, from a running language server (see lsp_start).

Reads the file's current on-disk content and opens/updates it on the language server automatically before requesting completions -- always pass the real current path; there is no separate "open" step to call yourself.

line/character are 0-indexed, the same convention every LSP client uses (line 0 is the first line; character 0 is the position before the first character of that line).

Returns a list of completion items (label, kind, detail, insertText).

Args:
    lsp_id: Handle returned by lsp_start
    path: File path (relative to the workspace, or absolute)
    line: 0-indexed line number
    character: 0-indexed character offset within the line

Returns:
    A formatted list of completion items, or "(no completions)"
"""

LSP_COMPLETION_PARAMETERS = {
    "type": "object",
    "properties": {
        "lsp_id": {"type": "string", "description": "Handle returned by lsp_start"},
        "path": {
            "type": "string",
            "description": "File path (relative to the workspace, or absolute)",
        },
        "line": {"type": "integer", "description": "0-indexed line number"},
        "character": {"type": "integer", "description": "0-indexed character offset within the line"},
    },
    "required": ["lsp_id", "path", "line", "character"],
}


LSP_STOP_DESCRIPTION = """
Stop a running language server started by lsp_start, freeing its resources.

Args:
    lsp_id: Handle returned by lsp_start

Returns:
    A confirmation message
"""

LSP_STOP_PARAMETERS = {
    "type": "object",
    "properties": {
        "lsp_id": {"type": "string", "description": "Handle returned by lsp_start"},
    },
    "required": ["lsp_id"],
}


def create_lsp_start_tool_spec(
    session_id: Optional[str] = None,
    sandbox_manager: Optional["SandboxManager"] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for lsp_start."""
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def lsp_start(language: str) -> str:
        if language not in _SUPPORTED_LANGUAGES:
            return f"Error: unsupported language {language!r}; supported: {', '.join(_SUPPORTED_LANGUAGES)}"

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            result = await resolved_manager.lsp_start(
                session_id=resolved_session_id, language=language
            )
        except Exception as e:
            logger.error(f"[lsp_start] Error starting {language} server: {e}", exc_info=True)
            return f"Error starting LSP server: {str(e)}"

        lsp_id = result.get("lsp_id")
        return f"Started {language} language server. lsp_id={lsp_id}"

    return ToolSpec(
        name="lsp_start",
        description=LSP_START_DESCRIPTION,
        parameters=LSP_START_PARAMETERS,
        handler=lsp_start,
    )


def create_lsp_completion_tool_spec(
    session_id: Optional[str] = None,
    sandbox_manager: Optional["SandboxManager"] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for lsp_completion.

    This is the one LSP tool that mirrors to AuditSink when configured --
    it's the actual "run code" moment of this feature (a real RPC
    round-trip to a real language-analysis process), the same
    classification the sidecar's own exec-budget wiring gives it (see
    sidecar_lsp.py's lsp_completion route docstring). lsp_start/lsp_stop
    don't audit, same as start_process/stop_process don't.
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

    async def lsp_completion(lsp_id: str, path: str, line: int, character: int) -> str:
        if not lsp_id or not lsp_id.strip():
            return "Error: lsp_id is required (call lsp_start first)"
        if not path or not path.strip():
            return "Error: path is required"

        start_time = time.monotonic()
        error: Optional[str] = None
        items: list[dict] = []
        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            view_result = await resolved_manager.view(session_id=resolved_session_id, path=path)
            content = view_result.get("content", "")

            await resolved_manager.lsp_open(
                session_id=resolved_session_id, lsp_id=lsp_id, path=path, content=content
            )
            completion_result = await resolved_manager.lsp_completion(
                session_id=resolved_session_id,
                lsp_id=lsp_id,
                path=path,
                line=line,
                character=character,
            )
            items = _simplify_completion_items(completion_result.get("items", []))
        except Exception as e:
            logger.error(f"[lsp_completion] Error: {e}", exc_info=True)
            error = str(e)

        duration_ms = int((time.monotonic() - start_time) * 1000)

        if audit_sink:
            await safe_call(
                audit_sink,
                "record_exec",
                organization_id=organization_id,
                work_item_id=work_item_id,
                session_id=str(session_id_uuid) if session_id_uuid else canonical_session_id,
                agent_name=agent_name,
                command=f"lsp_completion: {path}:{line}:{character}",
                exit_code=1 if error else 0,
                duration_ms=duration_ms,
            )

        if error:
            return f"Error getting completions: {error}"

        if not items:
            return "(no completions)"

        lines = []
        for item in items:
            detail_part = f" -- {item['detail']}" if item.get("detail") else ""
            lines.append(f"{item['label']} [{item['kind']}]{detail_part} -> {item['insertText']}")
        return "\n".join(lines)

    return ToolSpec(
        name="lsp_completion",
        description=LSP_COMPLETION_DESCRIPTION,
        parameters=LSP_COMPLETION_PARAMETERS,
        handler=lsp_completion,
    )


def create_lsp_stop_tool_spec(
    session_id: Optional[str] = None,
    sandbox_manager: Optional["SandboxManager"] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for lsp_stop."""
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def lsp_stop(lsp_id: str) -> str:
        if not lsp_id or not lsp_id.strip():
            return "Error: lsp_id is required"

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            await resolved_manager.lsp_stop(session_id=resolved_session_id, lsp_id=lsp_id)
        except Exception as e:
            logger.error(f"[lsp_stop] Error stopping {lsp_id}: {e}", exc_info=True)
            return f"Error stopping LSP server: {str(e)}"

        return f"Stopped LSP server {lsp_id}"

    return ToolSpec(
        name="lsp_stop",
        description=LSP_STOP_DESCRIPTION,
        parameters=LSP_STOP_PARAMETERS,
        handler=lsp_stop,
    )


def create_lsp_tool_specs(
    session_id: Optional[str] = None,
    sandbox_manager: Optional["SandboxManager"] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
) -> list[ToolSpec]:
    """Convenience bundle: all three lsp_* ToolSpecs (start, completion,
    stop), mirroring create_browser_tool_specs's own bundling pattern."""
    return [
        create_lsp_start_tool_spec(
            session_id=session_id,
            sandbox_manager=sandbox_manager,
            lazy_runtime=lazy_runtime,
        ),
        create_lsp_completion_tool_spec(
            session_id=session_id,
            sandbox_manager=sandbox_manager,
            lazy_runtime=lazy_runtime,
            organization_id=organization_id,
            work_item_id=work_item_id,
            agent_name=agent_name,
            audit_sink=audit_sink,
        ),
        create_lsp_stop_tool_spec(
            session_id=session_id,
            sandbox_manager=sandbox_manager,
            lazy_runtime=lazy_runtime,
        ),
    ]
