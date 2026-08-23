"""explain_last_failure: the last failed command, bundled with what it touched
(GitHub issue #77).

Scope note, because the issue's stated motivation only half applies here. It
argues the bundled output matters "since a normal exec result may have been
truncated for the model's sake" -- in this codebase it is not: `/exec` returns
stdout and stderr in full and `bash_tool` passes them straight through. What
the agent genuinely cannot get in one call is which files the failing command
touched, and that is the expensive part of the debugging loop this replaces.
Retaining the output alongside it is still worth the few KB, because by the
time an agent asks "why did that fail" the original result may have aged out
of its context.

Deletions are not reported. This works by scanning mtimes inside the command's
runtime window, and a deleted file has no mtime to find. Saying so is the
point: an empty deletions list that looked authoritative would be worse than
no list at all. `workspace_diff` (issue #71) does detect deletions, because it
compares snapshots rather than reading clocks.
"""

import asyncio
import logging
import os
import time
from typing import Optional

from fastapi import APIRouter, HTTPException

import main

logger = logging.getLogger("sidecar")

router = APIRouter()

# Generous rather than unlimited. A single command can emit gigabytes, and
# this is held for the whole session; anything failing with more than this
# much output has a different problem than the one this tool solves.
LAST_FAILURE_MAX_STREAM_BYTES = 256 * 1024

LAST_FAILURE_MAX_TOUCHED_FILES = 200

LAST_FAILURE_SCAN_TIMEOUT_SECONDS = 15.0

# Filesystem mtime granularity and the gap between "command exited" and "we
# read the clock" both make the window slightly fuzzy. Widening it risks
# attributing an unrelated write to this command; narrowing it risks missing
# a real one. A missed file is the worse failure for a debugging tool.
LAST_FAILURE_WINDOW_SLACK_SECONDS = 1.0

# One slot. The question is "why did *that* fail", asked immediately after,
# not a searchable history.
_last_failure: Optional[dict] = None


def _reset_state_for_tests() -> None:
    global _last_failure
    _last_failure = None


def record_failure(
    *,
    command: str,
    exit_code: int,
    stdout: str,
    stderr: str,
    started_at: float,
    ended_at: float,
    source: str = "exec",
) -> None:
    """Remember a nonzero-exit command.

    SECURITY: callers must pass the already-scrubbed streams. `/exec` runs
    `_scrub_secret_values` over stdout/stderr before building its response,
    and a value that was scrubbed out of the response must not survive in
    here to be handed back by a later call.
    """
    global _last_failure
    _last_failure = {
        "command": command,
        "exit_code": exit_code,
        "stdout": stdout[:LAST_FAILURE_MAX_STREAM_BYTES],
        "stderr": stderr[:LAST_FAILURE_MAX_STREAM_BYTES],
        "stdout_truncated": len(stdout) > LAST_FAILURE_MAX_STREAM_BYTES,
        "stderr_truncated": len(stderr) > LAST_FAILURE_MAX_STREAM_BYTES,
        "started_at": started_at,
        "ended_at": ended_at,
        "source": source,
    }


def _scan_touched_sync(start: float, end: float) -> tuple[list[dict], bool]:
    """Find files whose mtime falls inside the command's runtime window."""
    import sidecar_workspace_diff as wsdiff

    touched: list[dict] = []
    truncated = False
    roots = main._typed_allowed_roots()
    base = os.path.realpath(main.WORKSPACE_DIR)

    if not os.path.isdir(base):
        return [], False

    for root, dirnames, filenames in os.walk(base):
        dirnames[:] = [
            d for d in dirnames if d not in wsdiff.WORKSPACE_DIFF_DEFAULT_EXCLUDES
        ]
        for filename in filenames:
            full_path = os.path.join(root, filename)
            # Same per-entry containment re-check ls/glob/grep apply.
            if not main._is_path_contained(full_path, roots):
                continue
            if os.path.islink(full_path):
                continue
            try:
                st = os.stat(full_path, follow_symlinks=False)
            except OSError:
                continue
            if not (start <= st.st_mtime <= end):
                continue

            if len(touched) >= LAST_FAILURE_MAX_TOUCHED_FILES:
                truncated = True
                break
            touched.append({
                "path": main._to_virtual_path(full_path),
                "size_bytes": st.st_size,
                "modified_at": st.st_mtime,
            })
        if truncated:
            break

    touched.sort(key=lambda t: t["modified_at"], reverse=True)
    return touched, truncated


@router.post("/explain-last-failure", response_model=main.ExplainLastFailureResponse)
async def explain_last_failure(req: main.ExplainLastFailureRequest):
    """Return the most recent nonzero-exit command with the files it touched."""
    if _last_failure is None:
        return main.ExplainLastFailureResponse(
            found=False,
            notes=["No command has failed in this session yet."],
        )

    f = _last_failure
    notes: list[str] = []
    touched: list[dict] = []
    truncated = False

    if req.include_touched_files:
        start = f["started_at"] - LAST_FAILURE_WINDOW_SLACK_SECONDS
        end = f["ended_at"] + LAST_FAILURE_WINDOW_SLACK_SECONDS
        try:
            touched, truncated = await asyncio.wait_for(
                asyncio.to_thread(_scan_touched_sync, start, end),
                timeout=LAST_FAILURE_SCAN_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            notes.append(
                f"file scan timed out after {LAST_FAILURE_SCAN_TIMEOUT_SECONDS}s; "
                f"touched-file list is omitted, not empty"
            )
        else:
            notes.append(
                "touched files are those whose mtime falls inside the command's "
                "runtime window; deleted files cannot be detected this way -- "
                "use workspace_diff if you need deletions"
            )
            if truncated:
                notes.append(
                    f"more than {LAST_FAILURE_MAX_TOUCHED_FILES} files were touched; "
                    f"the list is capped"
                )

    if f["stdout_truncated"]:
        notes.append(f"stdout was capped at {LAST_FAILURE_MAX_STREAM_BYTES} bytes")
    if f["stderr_truncated"]:
        notes.append(f"stderr was capped at {LAST_FAILURE_MAX_STREAM_BYTES} bytes")

    return main.ExplainLastFailureResponse(
        found=True,
        command=f["command"],
        exit_code=f["exit_code"],
        stdout=f["stdout"],
        stderr=f["stderr"],
        duration_seconds=max(0.0, f["ended_at"] - f["started_at"]),
        source=f["source"],
        touched_files=[main.TouchedFile(**t) for t in touched],
        notes=notes,
    )
