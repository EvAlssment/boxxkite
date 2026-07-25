"""
Present Files Tool - Generate download URLs for users

This tool creates pre-signed S3/Azure URLs that allow users to download
files created by agents in the sandbox.

Files are stored in storage at:
  work-items/{org_id}/{work_item_id}/workspace/{path}  (work item sessions)
  work-items/{org_id}/{work_item_id}/outputs/{path}    (work item deliverables)
  sessions/{org_id}/{session_id}/workspace/{path}      (standalone sessions)

URLs are valid for 1 hour when an AuditSink is configured to generate them
(see ../audit.py); without one, files are still synced to storage and this
tool reports them as "ready for download" via their storage key.

Execution happens via SandboxManager HTTP calls to the sidecar container.

This module is framework-agnostic: `create_present_files_tool_spec()`
returns a plain `ToolSpec` (see ./types.py) with no LangChain import
anywhere in this file. `create_present_files_tool()` is a
backward-compatible wrapper that adapts that spec into a LangChain tool.
"""

import logging
from typing import Optional, TYPE_CHECKING
from uuid import UUID

from ..audit import AuditSink, safe_call
from ..lazy_runtime import resolve_sandbox_operation_context
from .types import ToolSpec

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

logger = logging.getLogger(__name__)


def _canonical_virtual_path(path: str) -> str:
    """Normalize user path input to sidecar virtual path form."""
    raw = (path or "").strip().replace("\\", "/")
    if not raw:
        return ""

    if not raw.startswith("/"):
        if raw in {"workspace", ".", ""}:
            return "/workspace"
        if raw.startswith("workspace/"):
            raw = raw[10:]
        while raw.startswith("./"):
            raw = raw[2:]
        return f"/workspace/{raw}" if raw else "/workspace"

    if raw == "/":
        return "/workspace"
    if raw == "/uploads" or raw.startswith("/uploads/"):
        suffix = raw[len("/uploads"):].lstrip("/")
        return f"/mnt/user-data/uploads/{suffix}" if suffix else "/mnt/user-data/uploads"
    if raw == "/outputs" or raw.startswith("/outputs/"):
        suffix = raw[len("/outputs"):].lstrip("/")
        return f"/mnt/user-data/outputs/{suffix}" if suffix else "/mnt/user-data/outputs"
    if raw == "/skills" or raw.startswith("/skills/"):
        suffix = raw[len("/skills"):].lstrip("/")
        return f"/mnt/skills/{suffix}" if suffix else "/mnt/skills"
    return raw.rstrip("/") or raw


def _work_item_file_path_from_virtual_path(virtual_path: str) -> Optional[str]:
    """
    Map sidecar virtual path to an AuditSink-facing work-item path.

    Returns:
        - workspace/{rel}
        - outputs/{rel}
        - uploads/{rel}
        - None for unsupported roots (skills, /tmp)
    """
    # Skip ephemeral /tmp paths - they shouldn't be synced or presented
    if virtual_path.startswith("/tmp/") or virtual_path == "/tmp":
        return None
    if virtual_path.startswith("/workspace/"):
        return f"workspace/{virtual_path[len('/workspace/'):]}"
    if virtual_path == "/workspace":
        return "workspace"
    if virtual_path.startswith("/mnt/user-data/outputs/"):
        return f"outputs/{virtual_path[len('/mnt/user-data/outputs/'):]}"
    if virtual_path == "/mnt/user-data/outputs":
        return "outputs"
    if virtual_path.startswith("/mnt/user-data/uploads/"):
        return f"uploads/{virtual_path[len('/mnt/user-data/uploads/'):]}"
    if virtual_path == "/mnt/user-data/uploads":
        return "uploads"
    return None


PRESENT_FILES_DESCRIPTION = """Generate download URLs for sandbox files.

Use this tool when you want users to be able to download files
you've created. Returns pre-signed URLs that are valid for 1 hour.

**Examples:**
- present_files(["report.csv"])
- present_files(["outputs/analysis.pdf", "outputs/summary.md"])

**Path format:**
- Use the same paths you used with file_create
- Relative paths are under /workspace
- Absolute paths can target /workspace or /mnt/user-data/outputs
- Non-output paths are copied into /mnt/user-data/outputs (flattened filename)

Args:
    paths: List of file paths to generate download URLs for

Returns:
    Formatted list of download URLs
"""

PRESENT_FILES_PARAMETERS = {
    "type": "object",
    "properties": {
        "paths": {
            "type": "array",
            "items": {"type": "string"},
            "description": "List of file paths to generate download URLs for",
        },
    },
    "required": ["paths"],
}


def create_present_files_tool_spec(
    audit_sink: Optional[AuditSink] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    chat_session_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for present_files.

    If an AuditSink is provided, presented files are also mirrored there
    (see ../audit.py) — this is optional and never blocks the sandbox sync.

    Returns:
        ToolSpec with a plain async handler(paths: list[str]) -> str
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def present_files(paths: list[str]) -> str:
        if not paths:
            return "Error: No file paths provided"

        results = []
        errors = []

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            # Execute via SandboxManager HTTP API
            present_result = await resolved_manager.present_files(
                session_id=resolved_session_id,
                filepaths=paths,
                include_operations=True,
            )
            if isinstance(present_result, dict):
                files = present_result.get("files", []) or []
                copy_operations = present_result.get("copy_operations", []) or []
            else:
                files = present_result
                copy_operations = []

            for file_info in files:
                file_path = file_info.get("file_path", "")
                storage_key = file_info.get("storage_key", file_info.get("s3_key", ""))
                size = file_info.get("size", 0)
                filename = file_path.split('/')[-1]

                normalized_path = _work_item_file_path_from_virtual_path(file_path)

                # Mirror the file registration to the optional AuditSink.
                # register_file_registered doesn't require re-uploading content —
                # it just tells the sink a file exists at this storage_key. This
                # matters for files created via bash_tool (e.g. a docx conversion)
                # that never went through file_create's own audit hook.
                if audit_sink and organization_id and work_item_id and normalized_path and storage_key:
                    await safe_call(
                        audit_sink,
                        "record_file_registered",
                        organization_id=organization_id,
                        work_item_id=work_item_id,
                        session_id=str(chat_session_id) if chat_session_id else None,
                        agent_name=agent_name,
                        file_path=normalized_path,
                        storage_key=storage_key,
                        size_bytes=size,
                    )

                # Ask the AuditSink for a signed download URL; None (no sink,
                # or sink declines) falls back to reporting the file as "ready".
                url = None
                if audit_sink and normalized_path and storage_key:
                    url = await safe_call(
                        audit_sink,
                        "get_download_url",
                        organization_id=organization_id,
                        work_item_id=work_item_id,
                        file_path=normalized_path,
                        storage_key=storage_key,
                        expiry_seconds=3600,
                    )

                if url:
                    results.append(f"- **{filename}** ({size} bytes): [Download]({url})")
                else:
                    results.append(f"- **{filename}** ({size} bytes): Ready for download")

            # Check for missing files
            returned_paths = {_canonical_virtual_path(str(f.get("file_path", ""))) for f in files}
            for path in paths:
                normalized = _canonical_virtual_path(path)
                if normalized not in returned_paths:
                    errors.append(f"- {path}: File not found or not synced")

        except Exception as e:
            logger.error(f"[present_files] Error with SandboxManager: {e}")
            return f"Error generating download URLs: {str(e)}"

        # Build response
        output_parts = []

        if copy_operations:
            output_parts.append("**Files Copied**:\n")
            for op in copy_operations:
                output_parts.append(f"- {op}")
            output_parts.append("")

        if results:
            output_parts.append("**Download Links** (valid for 1 hour):\n")
            output_parts.extend(results)

        if errors:
            output_parts.append("\n**Errors:**\n")
            output_parts.extend(errors)

        if not results and not errors:
            return "Error: No valid file paths provided"

        return "\n".join(output_parts)

    return ToolSpec(
        name="present_files",
        description=PRESENT_FILES_DESCRIPTION,
        parameters=PRESENT_FILES_PARAMETERS,
        handler=present_files,
    )


def create_present_files_tool(
    audit_sink: Optional[AuditSink] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    sandbox_manager: Optional['SandboxManager'] = None,
    session_id: Optional[str] = None,
    chat_session_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
):
    """
    Create present_files as a LangChain tool (backward-compatible wrapper).

    Prefer `create_present_files_tool_spec()` for framework-agnostic use.
    Requires the `langchain` extra (`pip install boxkite-sandbox[langchain]`).

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_present_files_tool_spec(
        audit_sink=audit_sink,
        organization_id=organization_id,
        work_item_id=work_item_id,
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        chat_session_id=chat_session_id,
        agent_name=agent_name,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]
