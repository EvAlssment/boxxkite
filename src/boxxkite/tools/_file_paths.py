"""Path and filename helpers shared by file_tools.py's tool handlers.

Split out of file_tools.py purely to keep that file under this repo's
per-file line budget — these are private (`_`-prefixed) helpers with no
independent public API, and no framework dependency of any kind.
"""

from __future__ import annotations

import mimetypes
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..manager import SandboxManager


_KNOWN_IMAGE_EXTENSIONS = {
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".tiff",
    ".tif",
    ".svg",
}


_SPACE_VARIANTS = {" ", " ", " ", " ", "⁠"}


def guess_image_mime_type(path: str) -> Optional[str]:
    """Best-effort image MIME type detection from path."""
    guessed, _ = mimetypes.guess_type(path)
    if guessed and guessed.startswith("image/"):
        return guessed

    ext = path.lower().rsplit("/", 1)[-1]
    if "." in ext:
        dot_ext = "." + ext.rsplit(".", 1)[-1]
        if dot_ext in _KNOWN_IMAGE_EXTENSIONS:
            return guessed or "image/png"
    return None


def extract_binary_file_hint(exc: Exception, path: str) -> Optional[str]:
    """If the error looks like a binary-file decode failure, return a helpful agent hint."""
    err_str = str(exc)
    # Match the 422 detail from sidecar, or a raw UnicodeDecodeError
    if "binary file" in err_str.lower() or "codec can't decode" in err_str:
        return (
            f"Error: '{path}' appears to be a binary file and cannot be viewed as text. "
            f"Use the exec tool with an appropriate Python library to read it "
            f"(e.g. pypdf for PDFs, openpyxl for Excel, python-docx for Word docs)."
        )
    return None


def normalize_space_variants(value: str) -> str:
    return "".join(
        " " if (ch == " " or ch in _SPACE_VARIANTS or ch.isspace()) else ch
        for ch in value
    )


def join_path(parent: str, name: str) -> str:
    if parent == "/":
        return f"/{name}"
    if parent.endswith("/"):
        return f"{parent}{name}"
    return f"{parent}/{name}"


async def resolve_path_with_space_fallback(
    *,
    sandbox_manager: "SandboxManager",
    session_id: Optional[str],
    path: str,
) -> Optional[str]:
    """
    Resolve file paths where normal spaces and Unicode no-break spaces were mixed.
    """
    candidate = path.rstrip("/")
    if not candidate:
        return None

    if "/" in candidate:
        parent, name = candidate.rsplit("/", 1)
        if not parent:
            parent = "/"
    else:
        parent, name = "/workspace", candidate

    if not name or not any((ch == " " or ch in _SPACE_VARIANTS or ch.isspace()) for ch in name):
        return None

    normalized_name = normalize_space_variants(name)

    try:
        parent_view = await sandbox_manager.view(
            session_id=session_id,
            path=parent,
            view_range=None,
            description="Resolve filename Unicode-space mismatch",
        )
    except Exception:
        return None

    if not isinstance(parent_view, dict) or not parent_view.get("is_directory"):
        return None

    entries = parent_view.get("entries", [])
    if not isinstance(entries, list):
        return None

    matches = [
        entry
        for entry in entries
        if isinstance(entry, str) and normalize_space_variants(entry) == normalized_name
    ]
    if not matches:
        return None

    # Prefer deterministic behavior if multiple files normalize to the same name.
    matches.sort()
    return join_path(parent, matches[0])


def normalize_to_workspace_path(path: str) -> Optional[str]:
    """
    Normalize a sandbox path to a workspace-relative path for database storage.

    Returns:
        - 'workspace/{path}' for paths under /workspace
        - 'outputs/{path}' for paths under /mnt/user-data/outputs
        - None for paths that shouldn't be synced (uploads, skills, /tmp, etc.)
    """
    path = path.strip()

    # Handle outputs directory
    if path.startswith('/mnt/user-data/outputs/'):
        return f"outputs/{path[len('/mnt/user-data/outputs/'):]}"
    if path.startswith('/mnt/user-data/outputs'):
        return "outputs"

    # Skip ephemeral /tmp (for throwaway scripts that don't need persistence)
    if path.startswith('/tmp/') or path == '/tmp':
        return None

    # Skip uploads (read-only) and skills (read-only)
    if path.startswith('/mnt/user-data/uploads') or path.startswith('/mnt/skills'):
        return None

    # Handle workspace paths
    if path.startswith('/workspace/'):
        return f"workspace/{path[len('/workspace/'):]}"
    if path.startswith('/workspace'):
        return "workspace"

    # Relative paths are under workspace
    if not path.startswith('/'):
        return f"workspace/{path}"

    # Unknown absolute path - skip sync
    return None
