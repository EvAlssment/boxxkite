"""File operations (create/view/read-image/str-replace/present-files),
search (ls/glob/grep), skills materialization (ensure-skills/inject-skills),
and the long-poll directory watcher (/watch).

Split out of the original monolithic ``main.py`` (GitHub issue #71) as a pure
mechanical refactor -- no behavior change. Path helpers, config, state, and
models remain owned by ``main`` and are referenced via ``main.<NAME>``.
Functions tests monkeypatch on ``main`` (``_grep_search_sync``,
``_revalidate_path_or_400``, ``_storage_bucket_for_virtual_path``,
``flush_outputs``) are called via ``main.`` so patches are observed.
"""

import asyncio
import base64
import ctypes
import ctypes.util
import hashlib
import io
import json
import logging
import mimetypes
import os
import re
import select
import shutil
import struct
import tempfile
import time as _time
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Optional
from uuid import uuid4

import aiofiles
from fastapi import APIRouter, HTTPException

import main

logger = logging.getLogger("sidecar")

router = APIRouter()


def _compute_skills_rev(skills: list[dict]) -> str:
    """
    Compute deterministic revision hash for skills payload.

    The hash is used by `/ensure-skills` to avoid unnecessary rewrites.
    """
    canonical_skills: list[dict] = []
    for raw_skill in skills:
        if not isinstance(raw_skill, dict):
            continue
        canonical_skill = {
            "instance_slug": str(raw_skill.get("instance_slug", "")),
            "display_name": str(raw_skill.get("display_name", "")),
            "description": str(raw_skill.get("description", "")),
            "source_type": str(raw_skill.get("source_type", "")),
            "skill_path": str(raw_skill.get("skill_path", "")),
        }
        entrypoints = raw_skill.get("entrypoints") or []
        canonical_skill["entrypoints"] = sorted(
            [
                {
                    "path": str(ep.get("path", "")),
                    "runtime": str(ep.get("runtime", "")),
                    "name": str(ep.get("name", "")),
                    "description": str(ep.get("description", "")),
                }
                for ep in entrypoints
                if isinstance(ep, dict)
            ],
            key=lambda ep: (ep["path"], ep["runtime"], ep["name"], ep["description"]),
        )
        files = raw_skill.get("files") or []
        canonical_skill["files"] = sorted(
            [
                {
                    "path": str(file_data.get("path", "")),
                    "content": str(file_data.get("content", "")),
                }
                for file_data in files
                if isinstance(file_data, dict)
            ],
            key=lambda file_data: file_data["path"],
        )
        canonical_skills.append(canonical_skill)

    canonical_skills.sort(key=lambda skill: skill["instance_slug"])
    encoded = json.dumps(canonical_skills, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def _materialize_skills(skills: list[dict]) -> tuple[int, int]:
    """Rebuild /mnt/skills from payload and lock down permissions."""
    os.makedirs(main.SKILLS_DIR, exist_ok=True)
    temp_dir = os.path.join(main.SKILLS_DIR, f".tmp-{uuid4().hex}")
    os.makedirs(temp_dir, exist_ok=True)

    skills_injected = 0
    files_written = 0

    try:
        for skill in skills:
            instance_slug = main._sanitize_instance_slug(str(skill.get("instance_slug", "")))
            skill_dir = os.path.join(temp_dir, instance_slug)
            os.makedirs(skill_dir, exist_ok=True)

            skill_files = skill.get("files", []) or []

            for file_data in skill_files:
                if not isinstance(file_data, dict):
                    continue
                rel_path = main._sanitize_rel_file_path(str(file_data.get("path", "")))
                content = str(file_data.get("content", ""))

                full_path = os.path.join(skill_dir, rel_path)
                resolved_full = os.path.realpath(full_path)
                resolved_skill_dir = os.path.realpath(skill_dir)
                if not resolved_full.startswith(resolved_skill_dir + os.sep):
                    raise HTTPException(status_code=400, detail=f"Unsafe path: {rel_path}")

                parent = os.path.dirname(full_path)
                if parent:
                    os.makedirs(parent, exist_ok=True)

                async with aiofiles.open(full_path, "w") as f:
                    await f.write(content)

                os.chmod(full_path, 0o444)
                files_written += 1

            for root, dirs, _ in os.walk(skill_dir):
                os.chmod(root, 0o555)
                for d in dirs:
                    os.chmod(os.path.join(root, d), 0o555)

            skills_injected += 1

        os.chmod(temp_dir, 0o555)

        temp_real = os.path.realpath(temp_dir)
        main._clear_directory_contents(main.SKILLS_DIR, keep_realpaths={temp_real})
        for entry in os.listdir(temp_dir):
            src = os.path.join(temp_dir, entry)
            dst = os.path.join(main.SKILLS_DIR, entry)
            os.replace(src, dst)
        os.rmdir(temp_dir)
        os.chmod(main.SKILLS_DIR, 0o555)

        return skills_injected, files_written
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


@router.post("/ensure-skills", response_model=main.EnsureSkillsResponse)
async def ensure_skills(req: main.EnsureSkillsRequest):
    """Ensure skills are materialized at /mnt/skills for the current session."""
    requested_rev = req.skills_rev or _compute_skills_rev(req.skills)
    current_rev = str(main.current_session.get("skills_rev") or "")

    if current_rev and current_rev == requested_rev and os.path.isdir(main.SKILLS_DIR):
        logger.info(f"[ensure-skills] No-op (unchanged rev={requested_rev[:12]})")
        return main.EnsureSkillsResponse(
            status="unchanged",
            changed=False,
            skills_rev=requested_rev,
            skills_injected=0,
            files_written=0,
        )

    logger.info(f"[ensure-skills] Rebuilding skills (rev={requested_rev[:12]}, count={len(req.skills)})")

    try:
        skills_injected, files_written = await _materialize_skills(req.skills)
        main.current_session["skills_rev"] = requested_rev
        main._scrub_disallowed_pending_sync()

        return main.EnsureSkillsResponse(
            status="updated",
            changed=True,
            skills_rev=requested_rev,
            skills_injected=skills_injected,
            files_written=files_written,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[ensure-skills] Error: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/inject-skills", response_model=main.EnsureSkillsResponse)
async def inject_skills_compat(req: main.EnsureSkillsRequest):
    """Compatibility alias for legacy callers."""
    return await ensure_skills(req)


@router.post("/file-create", response_model=main.FileCreateResponse)
async def file_create(req: main.FileCreateRequest):
    """
    Create or overwrite file on shared volume.

    No exec needed - writes directly to shared volume.
    Files are chown'd to sandbox user so sandbox can read/execute them.

    SECURITY: Writes to read-only mounts (/mnt/skills, /mnt/user-data/uploads) are blocked.
    """
    virtual_path, full_path = main._resolve_virtual_path(req.path)

    # SECURITY: Block writes to read-only roots.
    if main._is_read_only_virtual_path(virtual_path):
        raise HTTPException(
            status_code=403,
            detail=f"Cannot write to read-only path: {virtual_path}"
        )

    logger.info(f"[file-create] {full_path}")

    try:
        # Create parent directories
        dir_path = os.path.dirname(full_path)
        if dir_path:
            dir_path = main._revalidate_path_or_400(dir_path)
            os.makedirs(dir_path, exist_ok=True)
            # Set directory ownership to sandbox user
            dir_path = main._revalidate_path_or_400(dir_path)
            os.chown(dir_path, main.SANDBOX_UID, main.SANDBOX_GID)

        # Write content
        full_path = main._revalidate_path_or_400(full_path)
        async with aiofiles.open(full_path, 'w') as f:
            await f.write(req.content)
            # Set file ownership via the fd we already hold open, so there's
            # no further path-based race between the write and the chown.
            os.fchown(f.fileno(), main.SANDBOX_UID, main.SANDBOX_GID)

        size = len(req.content.encode('utf-8'))

        # Mark writeable namespaces for storage sync.
        if main._storage_bucket_for_virtual_path(virtual_path) is not None:
            main.pending_sync_files.add(virtual_path)

        return main.FileCreateResponse(
            path=virtual_path,
            size=size,
            created=True
        )

    except Exception as e:
        logger.error(f"[file-create] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/view", response_model=main.ViewResponse)
async def view_file(req: main.ViewRequest):
    """
    View file contents from shared volume.

    No exec needed - reads directly from shared volume.

    Path resolution:
    - Relative paths -> /workspace/{path}
    - /workspace/... -> working files (synced to storage)
    - /mnt/user-data/uploads/... -> uploaded files (read-only)
    - /mnt/user-data/outputs/... -> deliverables (synced to storage)
    - /mnt/skills/... -> immutable skills (read-only)
    - /tmp/... -> ephemeral scratch (NOT synced, for temp scripts)
    """
    try:
        _, full_path = main._resolve_virtual_path(req.path)
    except HTTPException as e:
        if e.status_code == 400:
            logger.warning(
                "[view] Bad request: path=%r view_range=%r description=%r detail=%s",
                req.path,
                req.view_range,
                req.description,
                e.detail,
            )
        raise

    logger.info(f"[view] {full_path}")

    full_path = main._revalidate_path_or_400(full_path)

    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail=f"File not found: {req.path}")

    # Check if directory
    if os.path.isdir(full_path):
        entries = os.listdir(full_path)
        return main.ViewResponse(
            content="",
            lines=0,
            is_directory=True,
            entries=entries
        )

    try:
        full_path = main._revalidate_path_or_400(full_path)

        file_size = os.stat(full_path).st_size
        if file_size > main.VIEW_MAX_FILE_SIZE_BYTES:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"'{req.path}' is {file_size} bytes, over the "
                    f"{main.VIEW_MAX_FILE_SIZE_BYTES} byte limit for /view. Use the exec "
                    "tool (e.g. head/tail/sed, or a streaming reader) to inspect part "
                    "of it instead of viewing the whole file."
                ),
            )

        async with aiofiles.open(full_path, 'r') as f:
            content = await f.read()

        lines = content.split('\n')
        total_lines = len(lines)

        # Apply line range if specified
        if req.view_range and len(req.view_range) == 2:
            start, end = req.view_range
            start = max(0, start - 1)  # Convert to 0-indexed
            end = min(total_lines, end)
            lines = lines[start:end]
            content = '\n'.join(lines)

        # Truncate if too large
        if len(content) > 100 * 1024:  # 100KB
            content = content[:100*1024] + "\n... (truncated)"

        return main.ViewResponse(
            content=content,
            lines=total_lines
        )

    except UnicodeDecodeError as e:
        logger.error(f"[view] Binary file cannot be read as text: {full_path}")
        raise HTTPException(
            status_code=422,
            detail=(
                f"'{req.path}' appears to be a binary file and cannot be viewed as text. "
                f"Use the exec tool with an appropriate Python library to read it "
                f"(e.g. pypdf for PDFs, openpyxl for Excel, python-docx for Word docs)."
            ),
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[view] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/read-image", response_model=main.ReadImageResponse)
async def read_image(req: main.ReadImageRequest):
    """
    Read image bytes from shared volume and return base64 payload.

    Used by sandbox-side image tools to send image content to vision-capable
    LLMs from backend.
    """
    _, full_path = main._resolve_virtual_path(req.path)
    logger.info(f"[read-image] {full_path}")

    full_path = main._revalidate_path_or_400(full_path)

    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail=f"File not found: {req.path}")

    if os.path.isdir(full_path):
        raise HTTPException(status_code=400, detail=f"Path is a directory: {req.path}")

    def _optimize_image_payload(
        image_bytes: bytes,
        source_mime_type: str,
    ) -> tuple[bytes, str]:
        """Downscale/recompress image bytes for LLM payload size control."""
        if not main.PILLOW_AVAILABLE:
            return image_bytes, source_mime_type

        try:
            with main.Image.open(io.BytesIO(image_bytes)) as img:
                img.load()
                source_w, source_h = img.size
                source_max_dim = max(source_w, source_h)
                resized = False

                if source_max_dim > main.READ_IMAGE_MAX_DIMENSION:
                    ratio = main.READ_IMAGE_MAX_DIMENSION / float(source_max_dim)
                    target_size = (
                        max(1, int(source_w * ratio)),
                        max(1, int(source_h * ratio)),
                    )
                    resample = (
                        main.Image.Resampling.LANCZOS
                        if hasattr(main.Image, "Resampling")
                        else main.Image.LANCZOS
                    )
                    img = img.resize(target_size, resample=resample)
                    resized = True

                # Preserve animated images as-is.
                if getattr(img, "is_animated", False):
                    return image_bytes, source_mime_type

                mime = source_mime_type.lower()
                out = io.BytesIO()

                if mime in {"image/jpeg", "image/jpg"}:
                    if img.mode not in {"RGB", "L"}:
                        img = img.convert("RGB")
                    img.save(
                        out,
                        format="JPEG",
                        quality=main.READ_IMAGE_JPEG_QUALITY,
                        optimize=True,
                    )
                    optimized = out.getvalue()
                    if not resized and len(optimized) >= len(image_bytes):
                        return image_bytes, "image/jpeg"
                    return optimized, "image/jpeg"

                if mime == "image/webp":
                    if img.mode not in {"RGB", "RGBA", "L"}:
                        img = img.convert("RGBA")
                    img.save(
                        out,
                        format="WEBP",
                        quality=main.READ_IMAGE_WEBP_QUALITY,
                        method=6,
                    )
                    optimized = out.getvalue()
                    if not resized and len(optimized) >= len(image_bytes):
                        return image_bytes, "image/webp"
                    return optimized, "image/webp"

                # Keep GIF as-is to avoid animation/frame conversion surprises.
                if mime == "image/gif":
                    return image_bytes, "image/gif"

                # Default output for png/unknown image types.
                if img.mode not in {"RGB", "RGBA", "L"}:
                    img = img.convert("RGBA")
                img.save(
                    out,
                    format="PNG",
                    optimize=True,
                    compress_level=main.READ_IMAGE_PNG_COMPRESS_LEVEL,
                )
                optimized = out.getvalue()
                if not resized and len(optimized) >= len(image_bytes):
                    return image_bytes, "image/png" if mime == "image/png" else source_mime_type
                return optimized, "image/png"
        except Exception as opt_err:
            logger.warning(f"[read-image] Optimization skipped (fallback to original): {opt_err}")
            return image_bytes, source_mime_type

    try:
        max_size_bytes = 50 * 1024 * 1024  # 50MB (matches upload limits)

        # Check size via stat() BEFORE reading -- an agent-writable file
        # under /workspace/etc. can be arbitrarily large (via /exec); reading
        # it fully into memory before this check would make the check
        # itself useless as a memory-exhaustion guard.
        size_bytes = os.stat(full_path).st_size
        if size_bytes > max_size_bytes:
            raise HTTPException(
                status_code=400,
                detail=f"Image too large: {size_bytes} bytes (max {max_size_bytes} bytes)"
            )

        async with aiofiles.open(full_path, "rb") as f:
            image_bytes = await f.read()

        if len(image_bytes) == 0:
            raise HTTPException(status_code=400, detail=f"Image is empty: {req.path}")

        # MIME detection from extension + magic bytes fallback
        mime_type, _ = mimetypes.guess_type(full_path)
        if not mime_type or not mime_type.startswith("image/"):
            if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
                mime_type = "image/png"
            elif image_bytes.startswith(b"\xff\xd8\xff"):
                mime_type = "image/jpeg"
            elif image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
                mime_type = "image/gif"
            elif image_bytes.startswith(b"RIFF") and image_bytes[8:12] == b"WEBP":
                mime_type = "image/webp"
            else:
                mime_type = mime_type or "application/octet-stream"

        if not mime_type.startswith("image/"):
            raise HTTPException(
                status_code=400,
                detail=f"File is not an image: {mime_type}"
            )

        payload_bytes, payload_mime_type = _optimize_image_payload(image_bytes, mime_type)
        payload_size_bytes = len(payload_bytes)
        if payload_size_bytes != size_bytes:
            logger.info(
                f"[read-image] Optimized {req.path}: {size_bytes} -> {payload_size_bytes} bytes "
                f"(mime {mime_type} -> {payload_mime_type})"
            )

        base64_data = base64.b64encode(payload_bytes).decode("ascii")
        return main.ReadImageResponse(
            path=req.path,
            mime_type=payload_mime_type,
            size_bytes=payload_size_bytes,
            base64_data=base64_data,
        )
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[read-image] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/str-replace", response_model=main.StrReplaceResponse)
async def str_replace(req: main.StrReplaceRequest):
    """
    Replace string in file on shared volume.

    No exec needed - reads/writes directly to shared volume.

    SECURITY: Edits to read-only mounts are blocked.
    """
    virtual_path, full_path = main._resolve_virtual_path(req.path)

    if main._is_read_only_virtual_path(virtual_path):
        raise HTTPException(
            status_code=403,
            detail=f"Cannot edit read-only path: {virtual_path}"
        )

    logger.info(f"[str-replace] {full_path}")

    full_path = main._revalidate_path_or_400(full_path)

    if not os.path.exists(full_path):
        raise HTTPException(status_code=404, detail=f"File not found: {req.path}")

    try:
        full_path = main._revalidate_path_or_400(full_path)

        file_size = os.stat(full_path).st_size
        if file_size > main.FILE_CONTENT_MAX_LENGTH:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"'{req.path}' is {file_size} bytes, over the "
                    f"{main.FILE_CONTENT_MAX_LENGTH} byte limit for /str-replace. Use the "
                    "exec tool (e.g. sed) to edit part of it instead."
                ),
            )

        async with aiofiles.open(full_path, 'r') as f:
            content = await f.read()

        # Count occurrences
        count = content.count(req.old_str)

        if count == 0:
            return main.StrReplaceResponse(path=virtual_path, replaced=False, occurrences=0)

        if count > 1 and not req.replace_all:
            raise HTTPException(
                status_code=400,
                detail=f"old_str appears {count} times. Must appear exactly once."
            )

        # Replace
        if req.replace_all:
            new_content = content.replace(req.old_str, req.new_str)
            replaced_count = count
        else:
            new_content = content.replace(req.old_str, req.new_str, 1)
            replaced_count = 1

        full_path = main._revalidate_path_or_400(full_path)
        # The existing file is owned by SANDBOX_UID (file_create chowns every
        # file it hands the sandbox). The sidecar runs as root but WITHOUT
        # CAP_DAC_OVERRIDE, so opening that file directly with 'w' fails EACCES
        # -- which is exactly why str_replace used to 500 -> 502 on every call
        # while file_create (which creates a brand-new root-owned file) worked.
        # Mirror file_create: write a fresh root-owned temp file, hand it to the
        # sandbox user, then atomically replace the target. os.replace only needs
        # write on the enclosing dir (the 0777 workspace), which the sidecar has,
        # and the swap is atomic so a reader never sees a half-written file.
        dir_name = os.path.dirname(full_path)
        orig_mode = os.stat(full_path).st_mode & 0o777
        tmp_fd, tmp_path = tempfile.mkstemp(dir=dir_name, prefix=".str-replace-")
        try:
            with os.fdopen(tmp_fd, "w") as f:
                f.write(new_content)
            # Set the mode BEFORE handing the file to SANDBOX_UID: the sidecar is
            # root but has no CAP_FOWNER, so once the temp is chown'd away from
            # root, chmod on it would EPERM. chmod-while-owner, then chown.
            os.chmod(tmp_path, orig_mode)
            try:
                os.chown(tmp_path, main.SANDBOX_UID, main.SANDBOX_GID)
            except OSError as chown_err:
                logger.warning(f"[str-replace] Skipped ownership set on {full_path}: {chown_err}")
            os.replace(tmp_path, full_path)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

        if main._storage_bucket_for_virtual_path(virtual_path) is not None:
            main.pending_sync_files.add(virtual_path)

        return main.StrReplaceResponse(path=virtual_path, replaced=True, occurrences=replaced_count)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[str-replace] Error: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/present-files", response_model=main.PresentFilesResponse)
async def present_files(req: main.PresentFilesRequest):
    """
    Ensure files are synced to storage and return file info.

    Triggers immediate sync for any pending files.
    """
    logger.info(f"[present-files] {req.filepaths}")

    if not main.current_session["storage_prefix"]:
        raise HTTPException(status_code=400, detail="No storage prefix configured for current session")

    files = []
    copy_operations: list[str] = []
    present_candidates: list[dict] = []
    requested_sync_paths: set[str] = set()
    for filepath in req.filepaths:
        virtual_path, full_path = main._resolve_virtual_path(filepath)

        if not os.path.exists(full_path):
            logger.warning(f"[present-files] File not found: {filepath}")
            continue
        if os.path.isdir(full_path):
            logger.warning(f"[present-files] Skipping directory path: {filepath}")
            continue

        # Claude parity: present_files copies non-output files into outputs.
        # - Copy (not move)
        # - Flatten to basename
        # - Silent overwrite
        present_virtual_path = virtual_path
        present_full_path = full_path
        outputs_root = os.path.realpath(main.OUTPUTS_DIR)

        if not main._is_under_root(os.path.realpath(full_path), outputs_root):
            filename = os.path.basename(full_path.rstrip("/"))
            if not filename:
                logger.warning(f"[present-files] Skipping path without filename: {filepath}")
                continue

            present_virtual_path = f"{main.OUTPUTS_DIR.rstrip('/')}/{filename}"
            _, present_full_path = main._resolve_virtual_path(present_virtual_path)

            # SECURITY: re-validate both paths immediately before the actual
            # filesystem syscalls -- `full_path` in particular was resolved
            # earlier in this loop iteration (see the top of this handler),
            # leaving a window where a backgrounded /exec process could swap
            # a path component to a symlink before this copy runs, turning
            # present-files into an exfiltration path to a user-downloadable
            # artifact. Matches the pattern already used by file_create/
            # str_replace/view/read_image.
            full_path = main._revalidate_path_or_400(full_path)
            present_full_path = main._revalidate_path_or_400(present_full_path)

            # Silent overwrite (last write wins).
            shutil.copy2(full_path, present_full_path)
            os.chown(present_full_path, main.SANDBOX_UID, main.SANDBOX_GID)

            copy_msg = f"Copied {virtual_path} to {present_virtual_path}"
            logger.info(f"[present-files] {copy_msg}")
            copy_operations.append(copy_msg)

        bucket_info = main._storage_bucket_for_virtual_path(present_virtual_path)
        if bucket_info is None:
            logger.warning(f"[present-files] Skipping unsyncable path: {present_virtual_path}")
            continue
        namespace, rel_path = bucket_info
        storage_key = f"{main.current_session['storage_prefix']}/{namespace}/{rel_path}"
        requested_sync_paths.add(present_virtual_path)
        present_candidates.append(
            {
                "virtual_path": present_virtual_path,
                "full_path": present_full_path,
                "storage_key": storage_key,
            }
        )

    ready_paths = await main.flush_outputs(
        reason="present-files",
        discover_untracked=False,
        requested_paths=requested_sync_paths,
    )

    for candidate in present_candidates:
        present_virtual_path = candidate["virtual_path"]
        if present_virtual_path not in ready_paths:
            if present_virtual_path in main.pending_sync_files:
                logger.warning(
                    f"[present-files] Requested file not ready yet (still pending): "
                    f"{present_virtual_path}"
                )
            else:
                logger.warning(
                    f"[present-files] Requested file not synced in this call: "
                    f"{present_virtual_path}"
                )
            continue

        present_full_path = candidate["full_path"]
        if not os.path.isfile(present_full_path):
            logger.warning(f"[present-files] File disappeared after sync: {present_virtual_path}")
            continue

        file_stat = os.stat(present_full_path)
        content_type = main._detect_content_type(present_full_path)

        files.append(
            {
                "file_path": present_virtual_path,
                "storage_key": candidate["storage_key"],
                "size": file_stat.st_size,
                "content_type": content_type,
            }
        )

    return main.PresentFilesResponse(
        files=files,
        copy_operations=copy_operations,
    )


@router.post("/ls", response_model=main.LsResponse)
async def ls_files(req: main.LsRequest):
    """List direct children under any allowed root path."""
    full_path = main._resolve_ls_path(req.path)
    logger.info(f"[ls] {full_path}")

    if not os.path.exists(full_path) or not os.path.isdir(full_path):
        return main.LsResponse(entries=[])

    entries: list[dict] = []
    ls_roots = main._ls_allowed_roots()
    try:
        for entry in os.scandir(full_path):
            try:
                is_dir = entry.is_dir(follow_symlinks=False)
                stat = entry.stat(follow_symlinks=False)
            except (OSError, PermissionError):
                continue

            # SECURITY: an agent-planted symlink (e.g. `ln -s /proc/self/environ
            # workspace/x` via /exec) resolves here to whatever it points at --
            # skip anything that resolves outside the allowed roots instead of
            # disclosing the resolved target path.
            if not main._is_path_contained(entry.path, ls_roots):
                continue

            # Keep returned paths canonical absolute filesystem paths so
            # callers can continue traversing under /mnt without remapping.
            virt_path = os.path.realpath(entry.path)
            if is_dir and not virt_path.endswith("/"):
                virt_path = virt_path + "/"

            entries.append(
                {
                    "path": virt_path,
                    "is_dir": is_dir,
                    "size": 0 if is_dir else int(stat.st_size),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
    except (OSError, PermissionError) as e:
        logger.error(f"[ls] Error listing {full_path}: {e}")
        return main.LsResponse(entries=[])

    entries.sort(key=lambda x: x.get("path", ""))
    return main.LsResponse(entries=entries)


@router.post("/glob", response_model=main.GlobResponse)
async def glob_files(req: main.GlobRequest):
    """Find files matching a glob pattern under any allowed root path."""
    import time as _time
    _t0 = _time.monotonic()
    _, base_path = main._resolve_virtual_path(req.path)
    logger.info(f"[glob] base={base_path}, pattern={req.pattern}")

    if not os.path.exists(base_path) or not os.path.isdir(base_path):
        return main.GlobResponse(matches=[])

    pattern = req.pattern.lstrip("/")
    if not pattern:
        return main.GlobResponse(matches=[])

    matches: list[dict] = []
    glob_roots = main._typed_allowed_roots()
    try:
        for matched in Path(base_path).rglob(pattern):
            try:
                if not matched.is_file():
                    continue
                stat = matched.stat()
            except (OSError, PermissionError):
                continue

            # SECURITY: see ls_files's matching comment -- an agent-planted
            # symlink can resolve outside the allowed roots; skip it rather
            # than disclose it (existence-oracle risk) or raise mid-loop.
            if not main._is_path_contained(str(matched), glob_roots):
                continue

            virt_path = main._to_virtual_path(str(matched))
            matches.append(
                {
                    "path": virt_path,
                    "is_dir": False,
                    "size": int(stat.st_size),
                    "modified_at": datetime.fromtimestamp(stat.st_mtime).isoformat(),
                }
            )
    except (ValueError, OSError) as e:
        logger.error(f"[glob] Error for pattern={pattern}: {e}")
        return main.GlobResponse(matches=[])

    matches.sort(key=lambda x: x.get("path", ""))
    logger.info(f"[TIMING] glob: {(_time.monotonic() - _t0)*1000:.0f}ms ({len(matches)} matches)")
    return main.GlobResponse(matches=matches)


def _grep_search_sync(
    base_path: str,
    regex: "re.Pattern",
    glob_pattern: Optional[str],
    max_matches: int,
    grep_roots: list[str],
) -> tuple[list[dict], bool]:
    """The actual walk+search, run in a worker thread (see grep_files) so a
    slow/stuck match doesn't block the event loop -- other requests (and the
    K8s health probe) keep working even if this thread doesn't return.
    """
    matches: list[dict] = []
    truncated = False
    bytes_scanned = 0

    if os.path.isfile(base_path):
        files_to_search = [base_path]
    else:
        files_to_search = []
        for root, _, filenames in os.walk(base_path):
            for filename in filenames:
                files_to_search.append(os.path.join(root, filename))

    for file_path in files_to_search:
        # SECURITY: an agent-planted symlink (e.g. via /exec) can resolve
        # outside the allowed roots -- skip it before opening rather than
        # reading it or raising (see _is_path_contained's docstring for why
        # raising here would be an exfiltration oracle, not just a crash).
        if not main._is_path_contained(file_path, grep_roots):
            continue

        if glob_pattern:
            base_for_rel = base_path if os.path.isdir(base_path) else os.path.dirname(base_path)
            rel_path = os.path.relpath(file_path, base_for_rel).replace(os.sep, "/")
            workspace_rel_path = main._to_virtual_path(file_path).lstrip("/")
            if not (
                fnmatch(rel_path, glob_pattern)
                or fnmatch(workspace_rel_path, glob_pattern)
                or fnmatch(os.path.basename(file_path), glob_pattern)
            ):
                continue
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                for line_num, line in enumerate(f, start=1):
                    bytes_scanned += len(line)
                    if regex.search(line):
                        matches.append(
                            {
                                "path": main._to_virtual_path(file_path),
                                "line": int(line_num),
                                "text": line.rstrip("\n"),
                            }
                        )
                        if len(matches) >= max_matches:
                            truncated = True
                            break
                    if bytes_scanned >= main.GREP_MAX_BYTES_SCANNED:
                        truncated = True
                        break
            if truncated:
                break
        except (UnicodeDecodeError, OSError, PermissionError):
            continue

    return matches, truncated


@router.post("/grep", response_model=main.GrepResponse)
async def grep_files(req: main.GrepRequest):
    """Search file content by regex pattern under any allowed root path."""
    try:
        regex = re.compile(req.pattern)
    except re.error as e:
        return main.GrepResponse(matches=[], error=f"Invalid regex pattern: {e}")

    search_path = req.path or "/"
    _, base_path = main._resolve_virtual_path(search_path)
    logger.info(f"[grep] base={base_path}, glob={req.glob}, max_matches={req.max_matches}")

    if not os.path.exists(base_path):
        return main.GrepResponse(matches=[])

    max_matches = max(1, min(req.max_matches, 5000))
    grep_roots = main._typed_allowed_roots()

    try:
        matches, truncated = await asyncio.wait_for(
            asyncio.to_thread(main._grep_search_sync, base_path, regex, req.glob, max_matches, grep_roots),
            timeout=main.GREP_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        logger.warning(f"[grep] Timed out after {main.GREP_TIMEOUT_SECONDS}s (base={base_path}, pattern={req.pattern!r})")
        return main.GrepResponse(
            matches=[],
            truncated=True,
            error=f"Search timed out after {main.GREP_TIMEOUT_SECONDS}s -- narrow the path or pattern and retry.",
        )

    return main.GrepResponse(matches=matches, truncated=truncated)


# ============================================================================
# Directory watcher (docs/FILE-WATCHER-DESIGN.md)
#
# A single-call, long-poll `inotify` watch: opens a watch, blocks up to
# `timeout_seconds` waiting for the FIRST batch of filesystem events under
# `path`, then closes the watch and returns whatever it saw (or an empty
# list on timeout). Deliberately NOT a persistent, cross-call watch --
# a change that happens in the gap between two calls (no watch open at
# that moment) is not reported. That's a real, documented limitation of
# this stateless shape, not an oversight: a persistent per-session watch
# handle would add another per-session resource to track and reap on
# teardown (the same "singleton, replaced on next call" pattern
# docs/FILE-WATCHER-DESIGN.md §3 flagged as the safer starting point,
# mirroring the one-active-interpreter/one-PTY-takeover pattern already
# used elsewhere in this codebase), which this first pass doesn't attempt.
#
# Uses ctypes to call inotify_init1/inotify_add_watch/inotify_rm_watch
# directly (stdlib has no inotify wrapper) rather than adding a new pip
# dependency for this one syscall family. Linux-only, matching every other
# namespace/capability operation in this file.
# ============================================================================

_LIBC = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)

IN_CREATE = 0x00000100
IN_MODIFY = 0x00000002
IN_DELETE = 0x00000200
IN_CLOSE_WRITE = 0x00000008
IN_MOVED_FROM = 0x00000040
IN_MOVED_TO = 0x00000080
_WATCH_EVENT_MASK = IN_CREATE | IN_MODIFY | IN_DELETE | IN_CLOSE_WRITE | IN_MOVED_FROM | IN_MOVED_TO
_INOTIFY_EVENT_FMT = "iIII"  # wd (int), mask (uint32), cookie (uint32), name_len (uint32)
_INOTIFY_EVENT_SIZE = struct.calcsize(_INOTIFY_EVENT_FMT)

_WATCH_EVENT_NAMES = {
    IN_CREATE: "created",
    IN_MODIFY: "modified",
    IN_DELETE: "deleted",
    IN_CLOSE_WRITE: "modified",  # a write followed by close -- report as "modified", not a separate bucket
    IN_MOVED_FROM: "moved_from",
    IN_MOVED_TO: "moved_to",
}

WATCH_DIRECTORY_MAX_TIMEOUT_SECONDS = 60


def _parse_inotify_events(data: bytes) -> list[tuple[int, str]]:
    """Parse a raw inotify read() buffer into (mask, name) pairs."""
    events: list[tuple[int, str]] = []
    pos = 0
    while pos + _INOTIFY_EVENT_SIZE <= len(data):
        _wd, mask, _cookie, name_len = struct.unpack_from(_INOTIFY_EVENT_FMT, data, pos)
        pos += _INOTIFY_EVENT_SIZE
        raw_name = data[pos : pos + name_len]
        pos += name_len
        name = raw_name.split(b"\x00", 1)[0].decode("utf-8", errors="replace")
        events.append((mask, name))
    return events


async def _watch_directory_once(full_path: str, timeout_seconds: float) -> tuple[list[dict], bool]:
    """Open an inotify watch on `full_path`, block (via a thread, so the
    event loop isn't blocked on the blocking read()/select() syscalls) up
    to `timeout_seconds` for the first batch of events, then tear the
    watch down. Returns (changes, timed_out)."""

    def _blocking_watch() -> tuple[list[dict], bool]:
        fd = _LIBC.inotify_init1(0)
        if fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        try:
            wd = _LIBC.inotify_add_watch(fd, full_path.encode("utf-8"), _WATCH_EVENT_MASK)
            if wd < 0:
                raise OSError(ctypes.get_errno(), f"inotify_add_watch failed for {full_path!r}")
            try:
                readable, _, _ = select.select([fd], [], [], timeout_seconds)
                if fd not in readable:
                    return [], True
                data = os.read(fd, 64 * 1024)
                changes = [
                    {"path": name, "event": _WATCH_EVENT_NAMES.get(mask, hex(mask))}
                    for mask, name in _parse_inotify_events(data)
                    if name  # a bare directory-level event (no name) isn't reportable as a file change
                ]
                return changes, False
            finally:
                _LIBC.inotify_rm_watch(fd, wd)
        finally:
            os.close(fd)

    return await asyncio.get_event_loop().run_in_executor(None, _blocking_watch)


@router.post("/watch", response_model=main.WatchDirectoryResponse)
async def watch_directory(req: main.WatchDirectoryRequest):
    """Long-poll for the first batch of filesystem changes under `path`.

    Reuses `_resolve_ls_path`'s exact same path-containment/allowed-roots
    check `/ls` already applies -- no new containment logic to get wrong.
    Read-only: no credentials, no new outbound network path, no privilege
    change -- see docs/FILE-WATCHER-DESIGN.md §4.
    """
    full_path = main._resolve_ls_path(req.path)
    if not os.path.isdir(full_path):
        raise HTTPException(status_code=400, detail=f"Not a directory: {req.path}")

    timeout_seconds = min(max(0.1, req.timeout_seconds), WATCH_DIRECTORY_MAX_TIMEOUT_SECONDS)

    try:
        raw_changes, timed_out = await _watch_directory_once(full_path, timeout_seconds)
    except OSError as e:
        logger.error(f"[watch] inotify error for {full_path}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to watch {req.path}: {e}")

    return main.WatchDirectoryResponse(
        changes=[main.WatchDirectoryChange(path=c["path"], event=c["event"]) for c in raw_changes],
        timed_out=timed_out,
    )
