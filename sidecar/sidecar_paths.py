"""Path resolution and containment helpers for the sidecar.

Split out of the original monolithic ``main.py`` (GitHub issue #71) as a pure
mechanical refactor -- no behavior change. Shared configuration remains owned
by ``main`` and is referenced here via ``main.<NAME>`` at call time so the
module attributes tests monkeypatch on ``main`` (e.g. ``WORKSPACE_DIR``) are
the ones this code reads.
"""

import os
from typing import Optional

from fastapi import HTTPException

import main


def _is_under_root(path: str, root: str) -> bool:
    """Return True when `path` is equal to or nested under `root`."""
    return path == root or path.startswith(root + os.sep)


def _normalize_input_path(path: str) -> str:
    """
    Normalize user-provided path to a canonical absolute path string.

    - Relative paths resolve under /workspace.
    - `/uploads/...`, `/outputs/...`, `/skills/...` map to `/mnt/...`.
    """
    raw = path.strip().replace("\\", "/")
    if not raw:
        raise HTTPException(status_code=400, detail="Path is required")

    # Backwards-compatible default for callers that pass "/" as workspace root.
    if raw == "/":
        return main.WORKSPACE_DIR

    if not raw.startswith("/"):
        # Relative paths are always workspace-relative.
        rel = raw
        if rel in {"workspace", ".", ""}:
            return main.WORKSPACE_DIR
        if rel.startswith("workspace/"):
            rel = rel[10:]
        while rel.startswith("./"):
            rel = rel[2:]
        return os.path.join(main.WORKSPACE_DIR, rel)

    # Convenience aliases.
    if raw == "/uploads" or raw.startswith("/uploads/"):
        suffix = raw[len("/uploads"):].lstrip("/")
        return os.path.join(main.UPLOADS_DIR, suffix) if suffix else main.UPLOADS_DIR
    if raw == "/outputs" or raw.startswith("/outputs/"):
        suffix = raw[len("/outputs"):].lstrip("/")
        return os.path.join(main.OUTPUTS_DIR, suffix) if suffix else main.OUTPUTS_DIR
    if raw == "/skills" or raw.startswith("/skills/"):
        suffix = raw[len("/skills"):].lstrip("/")
        return os.path.join(main.SKILLS_DIR, suffix) if suffix else main.SKILLS_DIR

    return raw


def _typed_allowed_roots() -> list[str]:
    """The 5 typed sandbox roots -- shared by _resolve_virtual_path,
    _path_root, and the per-entry containment re-checks in ls/glob/grep
    below (see _is_path_contained's docstring for why those need it too)."""
    return [
        os.path.realpath(main.WORKSPACE_DIR),
        os.path.realpath(main.UPLOADS_DIR),
        os.path.realpath(main.OUTPUTS_DIR),
        os.path.realpath(main.SKILLS_DIR),
        os.path.realpath(main.TMP_DIR),
    ]


def _ls_allowed_roots() -> list[str]:
    """Typed roots plus the /mnt hierarchy itself -- ls (unlike write/read
    endpoints) supports navigating /mnt so agents can discover mounted
    directories without alias remapping."""
    return _typed_allowed_roots() + [
        os.path.realpath("/mnt"),
        os.path.realpath("/mnt/user-data"),
    ]


def _is_path_contained(path: str, roots: list[str]) -> bool:
    """Non-raising containment check (unlike _revalidate_path_or_400): for
    ls/glob/grep's per-discovered-file loop, a single out-of-bounds entry
    (typically a symlink an agent planted via /exec, e.g. pointing at
    /proc/self/environ) must be silently skipped, not raise -- raising mid-
    loop both aborts an otherwise-legitimate request AND, worse, turns
    "did this path resolve outside the allowed roots" into an observable
    side channel (matched-content-found vs falls-outside-roots produce
    different HTTP outcomes), which an attacker can use to blind-exfiltrate
    file content one bit at a time without ever seeing the content itself."""
    resolved = os.path.realpath(path)
    return any(_is_under_root(resolved, root) for root in roots)


def _resolve_virtual_path(path: str) -> tuple[str, str]:
    """
    Resolve a user path into a canonical virtual path and absolute filesystem path.

    Allowed roots:
    - /workspace - Agent working files (synced to storage)
    - /mnt/user-data/uploads - User uploads (read-only)
    - /mnt/user-data/outputs - Agent deliverables (synced to storage)
    - /mnt/skills - Skill files (read-only)
    - /tmp - Ephemeral scratch space (NOT synced, for temp scripts)
    """
    normalized = _normalize_input_path(path)
    full_path = os.path.realpath(normalized)

    for root in _typed_allowed_roots():
        if _is_under_root(full_path, root):
            return _to_virtual_path(full_path), full_path

    raise HTTPException(status_code=400, detail=f"Invalid path outside allowed roots: {path}")


def _resolve_ls_path(path: str) -> str:
    """
    Resolve a user path for directory listing.

    Unlike write/read endpoints, ls supports navigating the /mnt hierarchy
    itself so agents can discover mounted directories without alias remapping.
    """
    normalized = _normalize_input_path(path)
    full_path = os.path.realpath(normalized)

    for root in _ls_allowed_roots():
        if _is_under_root(full_path, root):
            return full_path

    raise HTTPException(status_code=400, detail=f"Invalid path outside allowed roots: {path}")


def _path_root(abs_path: str) -> Optional[str]:
    resolved = os.path.realpath(abs_path)
    for root in _typed_allowed_roots():
        if _is_under_root(resolved, root):
            return root
    return None


def _revalidate_path_or_400(path: str) -> str:
    """Re-resolve and re-check `path` against the allowed roots.

    SECURITY: `_resolve_virtual_path` validates once, but a backgrounded
    process inside the sandbox can swap a path component to a symlink
    between that validation and the handler's actual filesystem syscalls
    (TOCTOU). Call this immediately before every syscall that touches the
    filesystem (makedirs/open/chown) so a swap performed in that window is
    caught rather than silently followed outside the allowed roots.
    """
    resolved = os.path.realpath(path)
    if _path_root(resolved) is None:
        raise HTTPException(status_code=400, detail=f"Invalid path outside allowed roots: {path}")
    return resolved


def _is_read_only_virtual_path(virtual_path: str) -> bool:
    """Block sidecar API writes (file_create, str_replace) to uploads/skills dirs.

    NOTE: This is independent of sync. Uploads ARE syncable to blob storage
    (mapped in _storage_bucket_for_virtual_path) — they're only read-only
    w.r.t. the sidecar write API.
    """
    abs_path = os.path.realpath(virtual_path)
    uploads_root = os.path.realpath(main.UPLOADS_DIR)
    skills_root = os.path.realpath(main.SKILLS_DIR)
    return _is_under_root(abs_path, uploads_root) or _is_under_root(abs_path, skills_root)


def _sanitize_instance_slug(slug: str) -> str:
    safe = slug.strip().replace("\\", "/").strip("/")
    if not safe:
        raise ValueError("instance_slug is required")
    if ".." in safe.split("/"):
        raise ValueError(f"Invalid instance_slug: {slug}")
    return safe


def _sanitize_rel_file_path(path: str) -> str:
    clean = path.strip().replace("\\", "/").lstrip("/")
    if not clean or clean.startswith("../") or "/../" in clean:
        raise ValueError(f"Invalid skill file path: {path}")
    return clean


def _to_virtual_path(full_path: str) -> str:
    """Convert an absolute filesystem path under allowed roots to canonical absolute virtual path."""
    abs_path = os.path.realpath(full_path)
    root = _path_root(abs_path)
    if root is None:
        raise HTTPException(status_code=400, detail=f"Path outside allowed roots: {full_path}")

    rel = os.path.relpath(abs_path, root)
    if rel == ".":
        return root
    return f"{root}/{rel}"


def _storage_bucket_for_virtual_path(virtual_path: str) -> Optional[tuple[str, str]]:
    """Map a virtual path to storage namespace and relative key."""
    abs_path = os.path.realpath(virtual_path)
    namespace_roots = [
        ("workspace", os.path.realpath(main.WORKSPACE_DIR)),
        ("outputs", os.path.realpath(main.OUTPUTS_DIR)),
        ("uploads", os.path.realpath(main.UPLOADS_DIR)),
    ]

    for namespace, root in namespace_roots:
        if _is_under_root(abs_path, root):
            rel = os.path.relpath(abs_path, root)
            rel = "" if rel == "." else rel
            return namespace, rel
    return None
