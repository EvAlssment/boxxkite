"""
File Tools - Create, view, and edit files in sandbox

These tools operate on the sandbox workspace filesystem:
- file_create: Create or overwrite files
- view: View file contents with line numbers
- str_replace: Replace unique strings in files

Files are stored in the sidecar's own S3/Azure storage at:
  work-items/{org_id}/{work_item_id}/workspace/{path}
  work-items/{org_id}/{work_item_id}/outputs/{path}

Execution happens via SandboxManager HTTP calls to the sidecar container.
An optional AuditSink (see ../audit.py) can mirror writes into an external
system of record; it is not required for these tools to work.

This module is framework-agnostic: `create_*_tool_spec()` factories return
plain `ToolSpec`s (see ./types.py) with no LangChain import anywhere in this
file. The `view` tool's image results are represented via `ToolImageResult`
rather than a LangChain content block/ToolMessage — the LangChain adapter
(./adapters.py) is the only place that constructs those. `create_*_tool()`
are backward-compatible wrappers that adapt the specs into LangChain tools.
"""

import logging
from typing import Optional, TYPE_CHECKING, Union
from uuid import UUID

from ..audit import AuditSink, safe_call
from ..lazy_runtime import resolve_sandbox_operation_context
from ._file_paths import (
    extract_binary_file_hint as _extract_binary_file_hint,
    guess_image_mime_type as _guess_image_mime_type,
    normalize_to_workspace_path as _normalize_to_workspace_path,
    resolve_path_with_space_fallback as _resolve_path_with_space_fallback,
)
from .types import ToolImageResult, ToolSpec

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

logger = logging.getLogger(__name__)


FILE_CREATE_DESCRIPTION = """Create or overwrite a file in the sandbox workspace.

Files created with this tool:
- Persist across chat sessions for the same work item (except /tmp)
- Can be downloaded by users via present_files
- Are synced to cloud storage (except /tmp)
- Appear in the work item's Files tab (except /tmp)

**Path format:**
- Relative paths are under /workspace (working files, synced)
- /mnt/user-data/outputs - deliverables for users (synced)
- /tmp - ephemeral scratch space (NOT synced, for throwaway scripts)
- Subdirectories are created automatically

**Examples:**
- file_create("analysis.py", "import pandas as pd\\n...")  # Synced
- file_create("/mnt/user-data/outputs/report.csv", "col1,col2\\n")  # Synced
- file_create("/tmp/temp_script.py", "# Throwaway script\\n...")  # NOT synced

Args:
    path: File path (relative to /workspace, or absolute /mnt/user-data/outputs or /tmp)
    content: File content as string

Returns:
    Success message with file path
"""

FILE_CREATE_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "File path (relative to /workspace, or absolute /mnt/user-data/outputs or /tmp)",
        },
        "content": {
            "type": "string",
            "description": "File content as string",
        },
    },
    "required": ["path", "content"],
}


def create_file_create_tool_spec(
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    session_id: Optional[Union[str, UUID]] = None,
    agent_name: Optional[str] = None,
    sandbox_manager: Optional['SandboxManager'] = None,
    audit_sink: Optional[AuditSink] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for file_create.

    Files are stored in the sidecar's own S3/Azure storage:
        work-items/{org_id}/{work_item_id}/workspace/{path}

    If an AuditSink is provided, successful writes are also mirrored there
    (see ../audit.py) — this is optional and never blocks the sandbox write.

    Returns:
        ToolSpec with a plain async handler(path, content) -> str
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    canonical_session_id = str(session_id).strip() if session_id else None
    if not canonical_session_id and lazy_runtime is not None:
        canonical_session_id = str(lazy_runtime.session_id).strip()
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
                f"[file_create] Non-UUID session_id={canonical_session_id!r}; "
                "DB audit session linkage will be omitted"
            )
            session_id_uuid = None

    async def file_create(path: str, content: str) -> str:
        # Validate path
        if not path or not path.strip():
            return "Error: File path is required"
        path = path.strip()

        # Validate content
        if content is None:
            return "Error: content parameter is required and cannot be empty"
        if not isinstance(content, str):
            return "Error: content parameter must be a string"
        if len(content) == 0:
            return "Error: content parameter is required and cannot be empty"

        # Validate content size (10MB limit)
        try:
            content_bytes = content.encode('utf-8')
        except Exception:
            return "Error: content must be valid UTF-8 text"

        if len(content_bytes) > 10 * 1024 * 1024:
            return (
                "Error: File content too large (>10MB). "
                "For large files, use bash_tool to generate them programmatically."
            )

        logger.info(f"[file_create] Creating {path} ({len(content_bytes)} bytes)")

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=canonical_session_id,
            )
            # Execute via SandboxManager HTTP API (creates in sidecar ephemeral storage)
            result = await resolved_manager.file_create(
                session_id=resolved_session_id,
                path=path,
                content=content,
            )

            # Mirror to the optional AuditSink (no-op if not configured)
            if audit_sink and organization_id and work_item_id:
                db_path = _normalize_to_workspace_path(path)
                if db_path:
                    await safe_call(
                        audit_sink,
                        "record_file_write",
                        organization_id=organization_id,
                        work_item_id=work_item_id,
                        session_id=str(session_id_uuid) if session_id_uuid else canonical_session_id,
                        agent_name=agent_name,
                        file_path=db_path,
                        content=content_bytes,
                    )

            return f"Created file: {result.get('path', path)} ({result.get('size', len(content_bytes))} bytes)"

        except Exception as e:
            logger.error(f"[file_create] Error: {e}", exc_info=True)
            return f"Error creating file: {str(e)}"

    return ToolSpec(
        name="file_create",
        description=FILE_CREATE_DESCRIPTION,
        parameters=FILE_CREATE_PARAMETERS,
        handler=file_create,
    )


def create_file_create_tool(
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    session_id: Optional[Union[str, UUID]] = None,
    agent_name: Optional[str] = None,
    sandbox_manager: Optional['SandboxManager'] = None,
    audit_sink: Optional[AuditSink] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
):
    """
    Create file_create as a LangChain tool (backward-compatible wrapper).

    Prefer `create_file_create_tool_spec()` for framework-agnostic use.
    Requires the `langchain` extra (`pip install boxkite-sandbox[langchain]`).

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_file_create_tool_spec(
        organization_id=organization_id,
        work_item_id=work_item_id,
        session_id=session_id,
        agent_name=agent_name,
        sandbox_manager=sandbox_manager,
        audit_sink=audit_sink,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]


VIEW_DESCRIPTION = """View the contents of a file in the sandbox workspace.

Returns file contents with line numbers (like 'cat -n').
Use start_line and end_line to view specific sections of large files.
For image files, call this same `view(path)` tool.
It returns multimodal image content for model vision.

**Path format:**
- Working files: 'output.csv', 'reports/analysis.md' (relative to /workspace)
- Uploaded files: '/mnt/user-data/uploads/filename.csv'
- Skill files: '/mnt/skills/{slug}/SKILL.md'
- Deliverables: '/mnt/user-data/outputs/file.ext'
- Temp files: '/tmp/script.py' (ephemeral, not synced)

**Examples:**
- view("analysis.py")  # View workspace file
- view("large_data.csv", start_line=1, end_line=50)  # First 50 lines
- view("/mnt/user-data/uploads/input.xlsx")  # View uploaded file
- view("/mnt/user-data/uploads/screenshot.png")  # View/analyze image file
- view("/tmp/temp_script.py")  # View ephemeral script

**For directories:**
- view("reports/")  # Lists directory contents

Args:
    path: File path to view
    start_line: Starting line number (1-indexed, default 1)
    end_line: Ending line number (default 100)

Returns:
    File contents with line numbers, or directory listing
"""

VIEW_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "File path to view",
        },
        "start_line": {
            "type": "integer",
            "description": "Starting line number (1-indexed, default 1)",
            "default": 1,
        },
        "end_line": {
            "type": "integer",
            "description": "Ending line number (default 100)",
            "default": 100,
        },
    },
    "required": ["path"],
}


def create_view_tool_spec(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for view.

    The handler returns either `str` (text content / directory listing /
    error message) or `ToolImageResult` (for image files) — see
    ./types.py. Framework-specific wrapping of the image case (e.g.
    LangChain's ToolMessage + injected tool_call_id) lives entirely in
    ./adapters.py, not here.

    Returns:
        ToolSpec with a plain async handler(path, start_line, end_line)
        -> Union[str, ToolImageResult]
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def view(
        path: str,
        start_line: int = 1,
        end_line: int = 100,
    ) -> Union[str, ToolImageResult]:
        # Validate path
        if not path or not path.strip():
            return "Error: File path is required"
        path = path.strip()

        logger.info(f"[view] Viewing {path} (lines {start_line}-{end_line})")
        resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
            lazy_runtime=lazy_runtime,
            sandbox_manager=sandbox_manager,
            session_id=session_id,
        )

        # Image paths return a framework-agnostic ToolImageResult.
        image_mime_type = _guess_image_mime_type(path)
        if image_mime_type:
            resolved_path = path
            try:
                image_result = await resolved_manager.read_image(
                    session_id=resolved_session_id,
                    path=resolved_path,
                    description="Read image for view tool multimodal output",
                )
            except FileNotFoundError:
                fallback_path = await _resolve_path_with_space_fallback(
                    sandbox_manager=resolved_manager,
                    session_id=resolved_session_id,
                    path=path,
                )
                if not fallback_path:
                    return f"Error reading image: File not found: {path}"
                resolved_path = fallback_path
                logger.info(
                    f"[view] Resolved image path with Unicode-space fallback: "
                    f"requested={path!r}, resolved={resolved_path!r}"
                )
                image_result = await resolved_manager.read_image(
                    session_id=resolved_session_id,
                    path=resolved_path,
                    description="Read image for view tool multimodal output",
                )
            except Exception as e:
                logger.error(f"[view] Failed to read image {path}: {e}", exc_info=True)
                return f"Error reading image: {str(e)}"

            b64_data = image_result.get("base64_data")
            if not b64_data:
                return f"Error: No image data returned for {path}"

            mime_type = str(image_result.get("mime_type") or image_mime_type or "image/png")
            if mime_type == "image/jpg":
                mime_type = "image/jpeg"

            return ToolImageResult(
                base64_data=b64_data,
                mime_type=mime_type,
                file_path=resolved_path,
            )

        resolved_path = path
        view_range = [start_line, end_line] if start_line > 1 or end_line != 100 else None
        try:
            result = await resolved_manager.view(
                session_id=resolved_session_id,
                path=resolved_path,
                view_range=view_range,
            )
        except Exception as e:
            fallback_path = await _resolve_path_with_space_fallback(
                sandbox_manager=resolved_manager,
                session_id=resolved_session_id,
                path=path,
            )
            if not fallback_path or fallback_path == resolved_path:
                logger.error(f"[view] Error: {e}", exc_info=True)
                hint = _extract_binary_file_hint(e, path)
                if hint:
                    return hint
                return f"Error viewing file: {str(e)}"

            resolved_path = fallback_path
            logger.info(
                f"[view] Resolved text path with Unicode-space fallback: "
                f"requested={path!r}, resolved={resolved_path!r}"
            )
            try:
                result = await resolved_manager.view(
                    session_id=resolved_session_id,
                    path=resolved_path,
                    view_range=view_range,
                )
            except Exception as resolved_error:
                logger.error(f"[view] Error: {resolved_error}", exc_info=True)
                hint = _extract_binary_file_hint(resolved_error, resolved_path)
                if hint:
                    return hint
                return f"Error viewing file: {str(resolved_error)}"

        # Handle directory listing
        if result.get("is_directory"):
            entries = result.get("entries", [])
            return f"Directory: {resolved_path}\n\n" + "\n".join(entries)

        content = result.get("content", "")
        total_lines = result.get("lines", 0)

        if not content:
            return f"File is empty: {resolved_path}"

        # Add line numbers
        lines = content.split('\n')
        numbered = []
        for i, line in enumerate(lines, start=start_line):
            numbered.append(f"{i:6d}\t{line}")

        return f"File: {resolved_path} (lines {start_line}-{min(end_line, total_lines)} of {total_lines})\n\n" + "\n".join(numbered)

    return ToolSpec(
        name="view",
        description=VIEW_DESCRIPTION,
        parameters=VIEW_PARAMETERS,
        handler=view,
        returns_multimodal=True,
    )


def create_view_tool(
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
):
    """
    Create view as a LangChain tool (backward-compatible wrapper).

    Prefer `create_view_tool_spec()` for framework-agnostic use — this
    still returns a LangChain-specific multimodal ToolMessage for images,
    matching prior behavior. Requires the `langchain` extra.

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_view_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]


STR_REPLACE_DESCRIPTION = """Replace a unique string in a file.

The old_str MUST appear exactly once in the file. If it appears multiple
times or not at all, the operation will fail. For multiple replacements,
use file_create to rewrite the entire file.

**Examples:**
- str_replace("config.py", "DEBUG = False", "DEBUG = True")
- str_replace("data.json", '"count": 0', '"count": 42')

Args:
    path: File path to edit (relative to /workspace or absolute writable path)
    old_str: Exact string to find (must appear exactly once)
    new_str: String to replace it with

Returns:
    Success message or error
"""

STR_REPLACE_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "File path to edit (relative to /workspace or absolute writable path)",
        },
        "old_str": {
            "type": "string",
            "description": "Exact string to find (must appear exactly once)",
        },
        "new_str": {
            "type": "string",
            "description": "String to replace it with",
        },
    },
    "required": ["path", "old_str", "new_str"],
}


def create_str_replace_tool_spec(
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    session_id: Optional[Union[str, UUID]] = None,
    agent_name: Optional[str] = None,
    sandbox_manager: Optional['SandboxManager'] = None,
    audit_sink: Optional[AuditSink] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for str_replace.

    Files are stored in the sidecar's own S3/Azure storage:
        work-items/{org_id}/{work_item_id}/workspace/{path}

    If an AuditSink is provided, successful edits are also mirrored there
    (see ../audit.py) — this is optional and never blocks the sandbox edit.

    Returns:
        ToolSpec with a plain async handler(path, old_str, new_str) -> str
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    canonical_session_id = str(session_id).strip() if session_id else None
    if not canonical_session_id and lazy_runtime is not None:
        canonical_session_id = str(lazy_runtime.session_id).strip()
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
                f"[str_replace] Non-UUID session_id={canonical_session_id!r}; "
                "DB audit session linkage will be omitted"
            )
            session_id_uuid = None

    async def str_replace(path: str, old_str: str, new_str: str) -> str:
        # Validate inputs
        if not path or not path.strip():
            return "Error: File path is required"
        path = path.strip()
        if not old_str:
            return "Error: old_str is required"
        if old_str == new_str:
            return "Error: old_str and new_str are identical"

        # Limit string sizes
        if len(old_str) > 100 * 1024:  # 100KB
            return (
                "Error: old_str too large for str_replace. "
                "Use file_create to rewrite the file instead."
            )

        logger.info(f"[str_replace] Editing {path}")

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=canonical_session_id,
            )
            # Execute via SandboxManager HTTP API
            result = await resolved_manager.str_replace(
                session_id=resolved_session_id,
                path=path,
                old_str=old_str,
                new_str=new_str,
            )

            if result.get("replaced"):
                # Mirror to the optional AuditSink (no-op if not configured)
                if audit_sink and organization_id and work_item_id:
                    db_path = _normalize_to_workspace_path(path)
                    if db_path:
                        try:
                            # Read the updated file content from sidecar
                            view_result = await resolved_manager.view(
                                session_id=resolved_session_id,
                                path=path,
                            )
                            updated_content = view_result.get("content", "")
                        except Exception as e:
                            logger.warning(f"[str_replace] Failed to read updated content for audit sink: {e}")
                            updated_content = ""
                        if updated_content:
                            await safe_call(
                                audit_sink,
                                "record_file_write",
                                organization_id=organization_id,
                                work_item_id=work_item_id,
                                session_id=str(session_id_uuid) if session_id_uuid else canonical_session_id,
                                agent_name=agent_name,
                                file_path=db_path,
                                content=updated_content.encode('utf-8'),
                            )

                return f"Replaced 1 occurrence in {path}"
            else:
                occurrences = result.get("occurrences", 0)
                if occurrences == 0:
                    return (
                        f"Error: old_str not found in {path}.\n"
                        f"Searched for: {repr(old_str[:200])}"
                    )
                else:
                    return (
                        f"Error: old_str appears {occurrences} times in {path}.\n"
                        f"str_replace requires the string to appear exactly once.\n"
                        f"Use file_create to rewrite the file if you need multiple replacements."
                    )

        except Exception as e:
            logger.error(f"[str_replace] Error: {e}", exc_info=True)
            return f"Error editing file: {str(e)}"

    return ToolSpec(
        name="str_replace",
        description=STR_REPLACE_DESCRIPTION,
        parameters=STR_REPLACE_PARAMETERS,
        handler=str_replace,
    )


def create_str_replace_tool(
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    session_id: Optional[Union[str, UUID]] = None,
    agent_name: Optional[str] = None,
    sandbox_manager: Optional['SandboxManager'] = None,
    audit_sink: Optional[AuditSink] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
):
    """
    Create str_replace as a LangChain tool (backward-compatible wrapper).

    Prefer `create_str_replace_tool_spec()` for framework-agnostic use.
    Requires the `langchain` extra (`pip install boxkite-sandbox[langchain]`).

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_str_replace_tool_spec(
        organization_id=organization_id,
        work_item_id=work_item_id,
        session_id=session_id,
        agent_name=agent_name,
        sandbox_manager=sandbox_manager,
        audit_sink=audit_sink,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]
