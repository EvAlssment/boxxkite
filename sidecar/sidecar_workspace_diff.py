"""Snapshot-based workspace diffing (GitHub issue #71).

`/watch` (docs/FILE-WATCHER-DESIGN.md) answers "what is changing right now":
it opens an inotify watch, blocks for the duration of one call, and is
explicitly blind to anything that happens between two calls. That leaves the
question an agent actually asks after doing some work -- "what changed since I
last looked" -- unanswerable in one call. Reconstructing it by hand costs
several ls/grep/view round-trips and still misses files the agent did not
touch itself.

This module answers that by comparing content snapshots, so a change made in
the gap between calls, by a background process, or by a previous turn is still
reported.

Everything here is bounded on purpose. A sandbox workspace can contain a
node_modules tree or a multi-gigabyte artifact, and neither the walk, the
retained snapshots, nor the emitted diff may grow with it. Every cap below has
a matching entry in the response's `notes`, because a result trimmed in
silence reads as "nothing else changed" -- the one wrong conclusion this tool
must never invite.
"""

import asyncio
import difflib
import hashlib
import logging
import os
import time
import uuid
from typing import Optional

from fastapi import APIRouter, HTTPException

import main

logger = logging.getLogger("sidecar")

router = APIRouter()

# Pruned by default: build/VCS/cache trees are enormous, change constantly for
# reasons no agent asked about, and drown the signal. `.git` in particular
# rewrites dozens of objects on every commit.
WORKSPACE_DIFF_DEFAULT_EXCLUDES = frozenset({
    ".git", ".hg", ".svn", "node_modules", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".venv", "venv", ".tox", "target",
    ".next", ".nuxt", "dist", "build", ".gradle", ".idea", ".cache",
})

# Content is retained for text files up to this size so a real unified diff
# can be produced later. Larger files fall back to hash-only identity: still
# correctly reported as modified, just without line-level detail.
WORKSPACE_DIFF_MAX_FILE_CONTENT_BYTES = 256 * 1024

# Hard ceiling on retained content per snapshot. Multiplied by
# WORKSPACE_DIFF_MAX_SNAPSHOTS below, this is the worst-case resident cost of
# the feature: 4 MiB x 4 = 16 MiB. Sandbox pods are memory-limited, and a
# convenience cache is not allowed to be the thing that OOMs one.
WORKSPACE_DIFF_MAX_SNAPSHOT_BYTES = 4 * 1024 * 1024

WORKSPACE_DIFF_MAX_FILES = 20_000

# Snapshots are retained per path so `checkpoint=None` can mean "since last
# time". Small and FIFO-evicted: this is a convenience cache, not storage.
WORKSPACE_DIFF_MAX_SNAPSHOTS = 4

WORKSPACE_DIFF_MAX_TOTAL_DIFF_BYTES = 256 * 1024

# A `rm -rf` or a dependency install can change thousands of files at once.
# Capping the diff bytes alone would still return thousands of entries, which
# is an agent's whole context window spent on a list it cannot act on.
WORKSPACE_DIFF_MAX_CHANGES = 500

WORKSPACE_DIFF_TIMEOUT_SECONDS = 30.0

# token -> snapshot dict. Insertion-ordered, so FIFO eviction is popping the
# oldest key. Process-local by design: a sidecar is per-sandbox, and a
# checkpoint from a previous pod is meaningless anyway.
_snapshots: dict[str, dict] = {}

# virtual path -> most recent token for that path, for the checkpoint=None case.
_latest_by_path: dict[str, str] = {}


def _reset_state_for_tests() -> None:
    _snapshots.clear()
    _latest_by_path.clear()


def _looks_binary(chunk: bytes) -> bool:
    return b"\x00" in chunk


def _take_snapshot_sync(base_path: str, excludes: frozenset) -> dict:
    """Walk `base_path` and record identity (and, where affordable, content).

    Runs in a worker thread: a large tree must not stall the event loop and
    the K8s health probe along with it, the same reason `_grep_search_sync`
    is threaded.
    """
    entries: dict[str, dict] = {}
    notes: list[str] = []
    retained_bytes = 0
    files_scanned = 0
    truncated = False

    roots = main._typed_allowed_roots()

    for root, dirnames, filenames in os.walk(base_path):
        # Prune in place so os.walk never descends into them at all -- the
        # point is not to filter node_modules out of the result, it is to
        # never pay for walking it.
        dirnames[:] = [d for d in dirnames if d not in excludes]

        for filename in filenames:
            if truncated:
                break
            full_path = os.path.join(root, filename)

            # SECURITY: same per-entry containment re-check ls/glob/grep do.
            # An agent can plant a symlink via /exec that resolves outside the
            # allowed roots; skip it silently rather than raising, because
            # raising turns containment into an observable side channel
            # (see _is_path_contained's docstring).
            if not main._is_path_contained(full_path, roots):
                continue

            try:
                st = os.stat(full_path, follow_symlinks=False)
            except OSError:
                continue
            if not os.path.isfile(full_path) or os.path.islink(full_path):
                continue

            files_scanned += 1
            if files_scanned > WORKSPACE_DIFF_MAX_FILES:
                truncated = True
                notes.append(
                    f"stopped after {WORKSPACE_DIFF_MAX_FILES} files; results are partial"
                )
                break

            virtual = main._to_virtual_path(full_path)
            entry: dict = {
                "size": st.st_size,
                "mtime_ns": st.st_mtime_ns,
                "sha256": None,
                "content": None,
                "binary": False,
            }

            try:
                with open(full_path, "rb") as fh:
                    if st.st_size <= WORKSPACE_DIFF_MAX_FILE_CONTENT_BYTES:
                        raw = fh.read()
                        entry["sha256"] = hashlib.sha256(raw).hexdigest()
                        entry["binary"] = _looks_binary(raw[:8192])
                        if (
                            not entry["binary"]
                            and retained_bytes + len(raw) <= WORKSPACE_DIFF_MAX_SNAPSHOT_BYTES
                        ):
                            try:
                                entry["content"] = raw.decode("utf-8")
                                retained_bytes += len(raw)
                            except UnicodeDecodeError:
                                entry["binary"] = True
                    else:
                        # Too big to retain: hash a bounded prefix plus size
                        # and mtime. Enough to detect change, not enough to
                        # diff -- and the response says which.
                        head = fh.read(65536)
                        entry["binary"] = _looks_binary(head[:8192])
                        entry["sha256"] = hashlib.sha256(
                            head + str(st.st_size).encode()
                        ).hexdigest()
            except OSError:
                continue

            entries[virtual] = entry

        if truncated:
            break

    if retained_bytes >= WORKSPACE_DIFF_MAX_SNAPSHOT_BYTES:
        notes.append(
            "snapshot content budget exhausted; some modified files will report "
            "no line-level diff"
        )

    return {
        "entries": entries,
        "notes": notes,
        "files_scanned": files_scanned,
        "truncated": truncated,
        "taken_at": time.time(),
    }


def _store_snapshot(virtual_base: str, snapshot: dict) -> str:
    token = f"wsdiff-{uuid.uuid4().hex[:16]}"
    snapshot["path"] = virtual_base
    _snapshots[token] = snapshot
    _latest_by_path[virtual_base] = token

    while len(_snapshots) > WORKSPACE_DIFF_MAX_SNAPSHOTS:
        oldest, evicted = next(iter(_snapshots.items()))
        del _snapshots[oldest]
        # Only clear the pointer if it still names the evicted snapshot;
        # a newer snapshot for the same path must keep its pointer.
        if _latest_by_path.get(evicted.get("path")) == oldest:
            _latest_by_path.pop(evicted.get("path"), None)

    return token


def _unified_diff(path: str, before: Optional[str], after: Optional[str], max_bytes: int) -> tuple[Optional[str], Optional[str]]:
    if before is None or after is None:
        return None, "content not retained (file too large or binary)"

    text = "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a{path}",
            tofile=f"b{path}",
            n=3,
        )
    )
    if not text:
        return None, None
    if len(text) > max_bytes:
        return text[:max_bytes] + f"\n... diff truncated at {max_bytes} bytes\n", None
    return text, None


def _compare(before: dict, after: dict, include_diffs: bool, max_diff_bytes: int) -> tuple[list[dict], list[str]]:
    old, new = before["entries"], after["entries"]
    changes: list[dict] = []
    notes: list[str] = []
    diff_budget = WORKSPACE_DIFF_MAX_TOTAL_DIFF_BYTES

    for path in sorted(set(old) | set(new)):
        o, n = old.get(path), new.get(path)

        if o is None:
            changes.append({
                "path": path, "change": "added", "size_bytes": n["size"],
                "size_delta_bytes": n["size"], "binary": n["binary"],
            })
            continue
        if n is None:
            changes.append({
                "path": path, "change": "removed", "size_bytes": 0,
                "size_delta_bytes": -o["size"], "binary": o["binary"],
            })
            continue

        # Hash first: a file rewritten with identical bytes, or merely
        # touched, is not a change the agent cares about. Falling back to
        # (size, mtime) only when a hash is missing on either side.
        if o["sha256"] is not None and n["sha256"] is not None:
            unchanged = o["sha256"] == n["sha256"]
        else:
            unchanged = o["size"] == n["size"] and o["mtime_ns"] == n["mtime_ns"]
        if unchanged:
            continue

        entry = {
            "path": path, "change": "modified", "size_bytes": n["size"],
            "size_delta_bytes": n["size"] - o["size"], "binary": n["binary"],
        }
        if include_diffs and not n["binary"] and not o["binary"]:
            if diff_budget <= 0:
                entry["diff_omitted_reason"] = "response diff budget exhausted"
            else:
                diff, reason = _unified_diff(
                    path, o["content"], n["content"], min(max_diff_bytes, diff_budget)
                )
                entry["diff"] = diff
                entry["diff_omitted_reason"] = reason
                if diff:
                    diff_budget -= len(diff)
        elif include_diffs:
            entry["diff_omitted_reason"] = "binary file"

        changes.append(entry)

    if diff_budget <= 0:
        notes.append(
            "total diff budget exhausted; later files list the change but not the diff"
        )

    if len(changes) > WORKSPACE_DIFF_MAX_CHANGES:
        dropped = len(changes) - WORKSPACE_DIFF_MAX_CHANGES
        changes = changes[:WORKSPACE_DIFF_MAX_CHANGES]
        notes.append(
            f"{dropped} further changed file(s) not listed (cap is "
            f"{WORKSPACE_DIFF_MAX_CHANGES}); narrow `path` or add noisy "
            f"directories to `exclude`"
        )
    return changes, notes


@router.post("/workspace-diff", response_model=main.WorkspaceDiffResponse)
async def workspace_diff(req: main.WorkspaceDiffRequest):
    """Report what changed under `path` since a previous snapshot.

    Read-only: no credentials, no outbound network, no privilege change. Reuses
    `_resolve_ls_path`'s containment check, then re-checks every discovered
    entry with `_is_path_contained` exactly as ls/glob/grep do.
    """
    base_path = main._resolve_ls_path(req.path)
    if not os.path.isdir(base_path):
        raise HTTPException(status_code=400, detail=f"Not a directory: {req.path}")

    excludes = WORKSPACE_DIFF_DEFAULT_EXCLUDES | set(req.exclude or ())
    virtual_base = main._to_virtual_path(base_path)

    try:
        snapshot = await asyncio.wait_for(
            asyncio.to_thread(_take_snapshot_sync, base_path, frozenset(excludes)),
            timeout=WORKSPACE_DIFF_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        raise HTTPException(
            status_code=504,
            detail=(
                f"Snapshot of {req.path} timed out after "
                f"{WORKSPACE_DIFF_TIMEOUT_SECONDS}s -- narrow the path or add "
                f"large directories to `exclude`."
            ),
        )

    if req.checkpoint:
        previous = _snapshots.get(req.checkpoint)
        if previous is None:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Unknown checkpoint {req.checkpoint!r}. Checkpoints are "
                    f"process-local and the oldest are evicted; call without a "
                    f"checkpoint to start a new baseline."
                ),
            )
    else:
        token = _latest_by_path.get(virtual_base)
        previous = _snapshots.get(token) if token else None

    token = _store_snapshot(virtual_base, snapshot)

    if previous is None:
        # First look at this path. Listing every existing file as "added"
        # would be a wall of noise that says nothing about what the agent
        # did, so establish the baseline and say so.
        return main.WorkspaceDiffResponse(
            checkpoint=token,
            baseline=True,
            changes=[],
            files_scanned=snapshot["files_scanned"],
            truncated=snapshot["truncated"],
            notes=snapshot["notes"] + [
                "baseline established; call again with this checkpoint to see changes"
            ],
        )

    changes, compare_notes = _compare(
        previous, snapshot, req.include_diffs, max(1024, req.max_diff_bytes)
    )

    logger.info(
        f"[workspace-diff] {virtual_base}: {len(changes)} change(s) "
        f"across {snapshot['files_scanned']} file(s)"
    )
    return main.WorkspaceDiffResponse(
        checkpoint=token,
        baseline=False,
        changes=[main.WorkspaceDiffEntry(**c) for c in changes],
        files_scanned=snapshot["files_scanned"],
        truncated=snapshot["truncated"],
        notes=snapshot["notes"] + compare_notes,
    )
