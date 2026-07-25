"""Storage sync/flush/prefetch machinery and the session-lifecycle routes
(/configure, /prefetch-uploads, /flush, /flush/confirmed).

Split out of the original monolithic ``main.py`` (GitHub issue #71) as a pure
mechanical refactor -- no behavior change. Shared state (``pending_sync_files``,
``synced_file_signatures``, ``_flush_lock``, ``_last_reconcile_at``,
``current_session``, ``_secret_value_cache``), config, and models remain owned
by ``main`` and are referenced via ``main.<NAME>``. Functions tests
monkeypatch on ``main`` (``flush_outputs``, ``prefetch_files``,
``_storage_bucket_for_virtual_path``) and cross-concern helpers
(``_kill_all_processes``, ``_reset_interpreter``, ``_reap_idle_interpreter``,
``_reset_node_interpreter``, ``_reap_idle_node_interpreter``,
``_reset_browser``, ``_reap_idle_browser``, ``_kill_all_lsp_servers``,
``_reap_idle_lsp_servers``, ``storage``,
``_to_virtual_path``) are called via ``main.`` so patches and the single
canonical instances are always observed.
"""

import asyncio
import logging
import os
import shutil
import stat
import time as _time
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, HTTPException

import main

logger = logging.getLogger("sidecar")

router = APIRouter()


def _get_flush_lock() -> asyncio.Lock:
    """Lazily create the flush lock in the active event loop."""
    if main._flush_lock is None:
        main._flush_lock = asyncio.Lock()
    return main._flush_lock


def _scrub_disallowed_pending_sync() -> None:
    """Drop pending sync entries whose paths have no storage mapping.

    Uses _storage_bucket_for_virtual_path (NOT _is_read_only_virtual_path).
    Uploads are read-only for API writes but are mapped for sync, so they
    pass through here and get flushed to blob storage normally.
    """
    to_remove = set()
    for path in main.pending_sync_files:
        if main._storage_bucket_for_virtual_path(path) is None:
            to_remove.add(path)
    if to_remove:
        for path in to_remove:
            main.pending_sync_files.discard(path)
        logger.info(f"[sync] Scrubbed {len(to_remove)} disallowed pending sync path(s)")


def _is_ignored_sync_dir_name(name: str) -> bool:
    return name in main.SYNC_IGNORE_DIRS


def _trim_synced_signature_cache() -> None:
    """Bound signature cache growth to avoid unbounded long-session memory usage."""
    size = len(main.synced_file_signatures)
    if size <= main.SYNC_SIGNATURE_MAX_ENTRIES:
        return

    to_remove = size - main.SYNC_SIGNATURE_MAX_ENTRIES
    for virtual_path in list(main.synced_file_signatures.keys())[:to_remove]:
        main.synced_file_signatures.pop(virtual_path, None)
    logger.warning(
        f"[sync] Trimmed signature cache by {to_remove} entries "
        f"(limit={main.SYNC_SIGNATURE_MAX_ENTRIES})"
    )


def _file_signature(full_path: str) -> Optional[tuple[int, int]]:
    """Return a stable file signature tuple: (size_bytes, mtime_ns)."""
    try:
        file_stat = os.stat(full_path, follow_symlinks=False)
    except (FileNotFoundError, PermissionError, OSError):
        return None

    if not stat.S_ISREG(file_stat.st_mode):
        return None

    return (
        int(file_stat.st_size),
        int(getattr(file_stat, "st_mtime_ns", int(file_stat.st_mtime * 1_000_000_000))),
    )


def _scan_sync_file_signatures() -> dict[str, tuple[int, int]]:
    """Walk sync roots and return latest signatures for all syncable files."""
    scanned: dict[str, tuple[int, int]] = {}
    # Uploads are included because sidecar prefetch can materialize new files
    # there without going through file-create/str-replace tracking.
    synced_roots = [main.WORKSPACE_DIR, main.OUTPUTS_DIR, main.UPLOADS_DIR]

    for root in synced_roots:
        root_real = os.path.realpath(root)
        if not os.path.isdir(root_real):
            continue

        for current_root, dirs, files in os.walk(root_real):
            # Ignore symlinked and explicitly ignored dirs.
            dirs[:] = [
                d
                for d in dirs
                if (
                    not os.path.islink(os.path.join(current_root, d))
                    and not _is_ignored_sync_dir_name(d)
                )
            ]

            for filename in files:
                full_path = os.path.join(current_root, filename)
                signature = _file_signature(full_path)
                if signature is None:
                    continue

                try:
                    virtual_path = main._to_virtual_path(full_path)
                except HTTPException:
                    continue

                if main._storage_bucket_for_virtual_path(virtual_path) is None:
                    continue

                scanned[virtual_path] = signature

    return scanned


async def _discover_untracked_sync_files() -> int:
    """
    Discover writable files under synced roots that were not explicitly tracked.

    Runs scanning in a worker thread to keep the sidecar event loop responsive.
    """
    scanned_signatures = await asyncio.to_thread(_scan_sync_file_signatures)

    discovered = 0
    for virtual_path, signature in scanned_signatures.items():
        # Already synced and unchanged.
        if main.synced_file_signatures.get(virtual_path) == signature:
            continue

        # Queue only files that are new/changed relative to last successful sync.
        if virtual_path not in main.pending_sync_files:
            main.pending_sync_files.add(virtual_path)
            discovered += 1

    # Prune signatures for files that no longer exist locally.
    pruned = 0
    for virtual_path in list(main.synced_file_signatures):
        if main._storage_bucket_for_virtual_path(virtual_path) is None:
            main.synced_file_signatures.pop(virtual_path, None)
            pruned += 1
            continue
        if virtual_path not in scanned_signatures:
            main.synced_file_signatures.pop(virtual_path, None)
            pruned += 1

    _trim_synced_signature_cache()

    if discovered:
        logger.info(f"[flush] Discovered {discovered} untracked sync file(s)")
    if pruned:
        logger.info(f"[sync] Pruned {pruned} stale signature entries")
    return discovered


def _clear_tmp_session_data() -> None:
    """
    Clear sandbox-owned entries in /tmp while preserving sidecar/system temp files.

    This prevents cross-session leakage when warm pods are recycled.
    """
    if not os.path.isdir(main.TMP_DIR):
        return

    removed = 0
    for name in os.listdir(main.TMP_DIR):
        child = os.path.join(main.TMP_DIR, name)
        try:
            child_stat = os.lstat(child)
        except FileNotFoundError:
            continue

        # Keep non-sandbox owned temp entries that may belong to the sidecar/runtime.
        if child_stat.st_uid != main.SANDBOX_UID:
            continue

        try:
            if os.path.isdir(child) and not os.path.islink(child):
                shutil.rmtree(child, ignore_errors=True)
            else:
                os.remove(child)
            removed += 1
        except FileNotFoundError:
            continue

    if removed:
        logger.info(f"[configure] Cleared {removed} sandbox-owned /tmp entries")


def _clear_directory_contents(path: str, keep_realpaths: Optional[set[str]] = None) -> None:
    """Remove all children under `path` while preserving the directory itself."""
    keep_realpaths = keep_realpaths or set()
    if not os.path.isdir(path):
        return

    for name in os.listdir(path):
        child = os.path.join(path, name)
        real_child = os.path.realpath(child)
        if real_child in keep_realpaths:
            continue
        try:
            if os.path.isdir(child) and not os.path.islink(child):
                shutil.rmtree(child, ignore_errors=True)
            else:
                os.remove(child)
        except FileNotFoundError:
            continue


@router.post("/configure", response_model=main.ConfigureResponse)
async def configure(req: main.ConfigureRequest):
    """
    Configure sidecar for a new session.

    Called when:
    1. Warm pod is claimed for a new session
    2. Cold pod starts with session assignment

    Actions:
    1. Wipe previous session data (workspace, outputs, uploads, /tmp sandbox files)
    2. Update session state
    3. Pre-fetch uploads and previous session files
    """
    import time as _time
    _t0 = _time.monotonic()
    logger.info(f"[configure] session={req.session_id}, work_item={req.work_item_id}")

    # SECURITY: kill every tracked background process *before* wiping
    # filesystem state below -- a process mid-write to disk at the moment of
    # a session transition must not race the wipe, and this is also the
    # mandatory fix for the cross-tenant leak this feature would otherwise
    # introduce into pod recycling (see docs/PROCESS-SESSIONS-DESIGN.md
    # sections 2(b)/5 and _kill_all_processes()'s docstring). This runs on
    # every /configure call, not just the identity-wiping recycle payload --
    # SandboxManager also calls /process/kill-all explicitly before
    # /configure as an additional, non-redundant safeguard.
    await main._kill_all_processes()
    # Same requirement, same reason, for the persistent takeover tmux
    # session (GitHub issue #130): tmux surviving a dropped WebSocket is
    # the whole point of that feature, but it must NOT survive a pod
    # recycle into a different tenant's session -- see
    # kill_takeover_tmux_session's own docstring.
    await main.kill_takeover_tmux_session()
    # Kill any live persistent interpreter BEFORE wiping session data, not
    # after -- a recycled pod handing a new tenant a still-live interpreter
    # (and its globals) from the previous tenant is a cross-tenant data leak
    # by construction, the same class of bug docs/PROCESS-SESSIONS-DESIGN.md
    # §2(b) calls out for kept-alive background processes generally.
    await main._reset_interpreter()
    # Same requirement for the (opt-in, may not even be running) Node
    # interpreter -- always call this, not just when
    # BOXKITE_NODE_INTERPRETER_ENABLED is set, since a still-live process
    # started while the flag was on must still be killed if the flag was
    # since flipped off before this recycle.
    await main._reset_node_interpreter()
    # Same requirement for the (opt-in, may not even be running) browser --
    # a recycled pod must never hand a new tenant a still-live browser page
    # (cookies, session storage, whatever the previous tenant navigated to)
    # left over from before. Always call this, not just when
    # BOXKITE_BROWSER_ENABLED is set, same reasoning as the Node interpreter
    # reset above (docs/BROWSER-EXEC-DESIGN.md §4).
    await main._reset_browser()
    # Same requirement for the (opt-in) desktop takeover stack -- a recycled
    # pod must never hand a new tenant a still-live Xvfb/WM/x11vnc session
    # (windows, clipboard, whatever the previous tenant had open) left over
    # from before. Always call this, not just when BOXKITE_DESKTOP_ENABLED is
    # set, same reasoning as _reset_browser above.
    await main.kill_desktop_session()
    # Same requirement for the (opt-in, may not even be running) LSP
    # servers (GitHub issue #183) -- a recycled pod must never hand a new
    # tenant a still-live language server whose open documents contain the
    # previous tenant's source code. Always call this, not just when
    # BOXKITE_LSP_ENABLED is set, same reasoning as the Node interpreter/
    # browser resets above.
    await main._kill_all_lsp_servers()

    # Serialize configure against active flushes to avoid session-transition races.
    async with _get_flush_lock():
        # Wipe previous data
        if os.path.exists(main.WORKSPACE_DIR):
            shutil.rmtree(main.WORKSPACE_DIR, ignore_errors=True)
        if os.path.exists(main.OUTPUTS_DIR):
            shutil.rmtree(main.OUTPUTS_DIR, ignore_errors=True)
        if os.path.exists(main.UPLOADS_DIR):
            shutil.rmtree(main.UPLOADS_DIR, ignore_errors=True)
        _clear_tmp_session_data()
        # Reset skills directory only when switching sessions.
        if main.current_session.get("session_id") and req.session_id != main.current_session.get("session_id"):
            _clear_directory_contents(main.SKILLS_DIR)
            main.current_session["skills_rev"] = None

        _t1 = _time.monotonic()
        logger.info(f"[TIMING] configure_wipe: {(_t1 - _t0)*1000:.0f}ms")

        # Create directories with proper ownership for sandbox user
        os.makedirs(main.WORKSPACE_DIR, exist_ok=True)
        os.makedirs(main.OUTPUTS_DIR, exist_ok=True)
        os.makedirs(main.UPLOADS_DIR, exist_ok=True)
        os.makedirs(main.SKILLS_DIR, exist_ok=True)
        os.makedirs(main.TMP_DIR, exist_ok=True)
        os.chmod(main.SKILLS_DIR, 0o555)

        # Set ownership so sandbox container can write to workspace and outputs.
        os.chown(main.WORKSPACE_DIR, main.SANDBOX_UID, main.SANDBOX_GID)
        os.chown(main.OUTPUTS_DIR, main.SANDBOX_UID, main.SANDBOX_GID)
        # uploads and skills stay root-owned (read-only for sandbox).

        # Clear sync state.
        main.pending_sync_files.clear()
        main.synced_file_signatures.clear()

        # Update session state
        # Use new unified storage prefix: work-items/{org_id}/{work_item_id}
        # Fallback only when both IDs are available.
        storage_prefix = req.storage_prefix
        if not storage_prefix and req.organization_id and req.work_item_id:
            storage_prefix = f"work-items/{req.organization_id}/{req.work_item_id}"
        main.current_session.update({
            "session_id": req.session_id,
            "organization_id": req.organization_id,
            "work_item_id": req.work_item_id,
            "storage_prefix": storage_prefix,
            "skills_rev": main.current_session.get("skills_rev"),
            "configured_at": datetime.now().isoformat(),
            "secret_names": list(req.secret_names or []),
            "secret_allowed_hosts": dict(req.secret_allowed_hosts or {}),
            "secret_capability_token": req.secret_capability_token,
            "secrets_control_plane_url": req.secrets_control_plane_url,
        })
        # A recycled pod must never serve a previous tenant's cached secret
        # value to the new session -- see _secret_value_cache's docstring.
        main._secret_value_cache.clear()

        # A recycled pod must never carry a previous tenant's exec budget
        # usage (or a stale breach) into the new session -- see issue #122.
        main._reset_session_exec_budget()

        main._last_reconcile_at = 0.0

    # Pre-fetch files from storage
    prefetched = []
    _t2 = _time.monotonic()
    if storage_prefix:
        prefetched.extend(await main.prefetch_files(storage_prefix))
    if req.upload_file_ids and req.organization_id:
        prefetched.extend(
            await prefetch_legacy_uploads(
                organization_id=req.organization_id,
                upload_file_ids=req.upload_file_ids,
            )
        )
    if prefetched:
        prefetched = list(dict.fromkeys(prefetched))

    _t3 = _time.monotonic()
    logger.info(f"[TIMING] configure_prefetch: {(_t3 - _t2)*1000:.0f}ms ({len(prefetched)} files)")
    logger.info(f"[TIMING] configure_total: {(_t3 - _t0)*1000:.0f}ms")

    return main.ConfigureResponse(
        status="configured",
        session_id=req.session_id,
        prefetched_files=prefetched
    )


@router.post("/prefetch-uploads", response_model=main.PrefetchUploadsResponse)
async def prefetch_uploads(req: main.PrefetchUploadsRequest):
    """
    Pre-fetch upload files without wiping workspace/session state.

    Used by compose-session reuse flows so newly attached uploads are available
    in /mnt/user-data/uploads for view/bash access.
    """
    prefetched = []

    storage_prefix = main.current_session.get("storage_prefix")
    if storage_prefix:
        prefetched.extend(await prefetch_uploads_from_prefix(storage_prefix))

    org_id = req.organization_id or main.current_session.get("organization_id")
    if req.upload_file_ids and org_id:
        prefetched.extend(
            await prefetch_legacy_uploads(
                organization_id=org_id,
                upload_file_ids=req.upload_file_ids,
            )
        )

    if prefetched:
        prefetched = list(dict.fromkeys(prefetched))

    return main.PrefetchUploadsResponse(
        status="prefetched",
        session_id=main.current_session.get("session_id"),
        prefetched_files=prefetched,
    )


@router.post("/flush")
async def flush_endpoint():
    """Manually trigger flush of pending sync files to storage."""
    await main.flush_outputs(reason="endpoint", discover_untracked=True)
    return {"status": "flushed", "files": list(main.pending_sync_files)}


@router.post("/flush/confirmed")
async def confirmed_flush_endpoint():
    """Flush pending outputs and return the confirmed, durably-uploaded
    manifest -- unlike /flush (which echoes `pending_sync_files`, the
    *before* state), this returns `flush_outputs()`'s own `ready` set: files
    this sidecar has verified are now actually present in storage after
    upload, which is the "as-of" boundary a filesystem snapshot needs to be
    real (see docs/SNAPSHOT-DESIGN.md section 4).

    `storage_keys` are expressed relative to `storage_prefix`
    (`{namespace}/{rel_path}`, e.g. "workspace/foo.py") using the exact same
    virtual-path -> storage-key mapping `_sync_candidate_paths` already uses
    to build the real upload key -- callers (SandboxManager, and in turn the
    control plane) can address these directly for a storage-side copy
    without re-deriving that mapping themselves.
    """
    ready = await main.flush_outputs(reason="confirmed-flush", discover_untracked=True)
    storage_prefix = main.current_session.get("storage_prefix")
    storage_keys: list[str] = []
    for virtual_path in sorted(ready):
        bucket_info = main._storage_bucket_for_virtual_path(virtual_path)
        if bucket_info is None:
            continue
        namespace, rel_path = bucket_info
        storage_keys.append(f"{namespace}/{rel_path}" if rel_path else namespace)
    return {
        "status": "flushed",
        "storage_prefix": storage_prefix,
        "storage_keys": storage_keys,
    }


# ============================================================================
# Background Tasks
# ============================================================================

def _has_active_sync_session() -> bool:
    return bool(main.current_session.get("storage_prefix"))


def _queue_virtual_path_for_sync(virtual_path: str) -> bool:
    """Add a canonical virtual path to the pending sync set if syncable."""
    if main._storage_bucket_for_virtual_path(virtual_path) is None:
        return False
    main.pending_sync_files.add(virtual_path)
    return True


async def _wait_for_stable_signature(
    full_path: str,
    initial_signature: tuple[int, int],
) -> Optional[tuple[int, int]]:
    """Double-check file signature before upload to avoid syncing mid-write files.

    Waits SYNC_STABLE_CHECK_INTERVAL_MS then re-stats the file.  If the
    (size, mtime_ns) tuple changed, returns None to signal "file still being
    written — skip this cycle and retry later".
    """
    if main.SYNC_STABLE_CHECK_INTERVAL_MS <= 0:
        return initial_signature

    await asyncio.sleep(main.SYNC_STABLE_CHECK_INTERVAL_MS / 1000.0)
    latest = _file_signature(full_path)
    if latest is None:
        return None
    if latest != initial_signature:
        return None
    return latest


async def _sync_candidate_paths(
    candidate_paths: set[str],
    storage_prefix: str,
    *,
    reason: str,
) -> set[str]:
    """Upload candidate files to blob storage, returning the set of confirmed paths.

    For each file:
    1. Check if already synced and unchanged (signature match) → skip.
    2. Wait for stable signature → skip if file is mid-write.
    3. Upload to storage.
    4. Re-check signature after upload → if changed mid-upload, keep pending.
    """
    ready: set[str] = set()
    uploaded_count = 0

    for virtual_path in sorted(candidate_paths):
        bucket_info = main._storage_bucket_for_virtual_path(virtual_path)
        if bucket_info is None:
            main.pending_sync_files.discard(virtual_path)
            continue

        namespace, rel_path = bucket_info
        full_path = os.path.realpath(virtual_path)
        if not os.path.isfile(full_path):
            main.pending_sync_files.discard(virtual_path)
            main.synced_file_signatures.pop(virtual_path, None)
            continue

        signature = _file_signature(full_path)
        if signature is None:
            main.pending_sync_files.discard(virtual_path)
            main.synced_file_signatures.pop(virtual_path, None)
            continue

        # Already synced and unchanged.
        if main.synced_file_signatures.get(virtual_path) == signature:
            main.pending_sync_files.discard(virtual_path)
            ready.add(virtual_path)
            continue

        stable_signature = await _wait_for_stable_signature(full_path, signature)
        if stable_signature is None:
            # Keep pending; a later cycle should retry once writes settle.
            main.pending_sync_files.add(virtual_path)
            continue

        storage_key = f"{storage_prefix}/{namespace}/{rel_path}"
        if not await main.storage().upload(full_path, storage_key):
            # Keep pending for next cycle.
            main.pending_sync_files.add(virtual_path)
            continue

        post_upload_signature = _file_signature(full_path)
        if post_upload_signature is None:
            main.pending_sync_files.discard(virtual_path)
            main.synced_file_signatures.pop(virtual_path, None)
            continue

        if post_upload_signature != stable_signature:
            # The file changed mid-upload; keep it pending for a clean retry.
            main.pending_sync_files.add(virtual_path)
            continue

        uploaded_count += 1
        ready.add(virtual_path)
        main.pending_sync_files.discard(virtual_path)
        main.synced_file_signatures[virtual_path] = post_upload_signature

    if uploaded_count:
        logger.info(f"[flush:{reason}] Synced {uploaded_count} file(s) to storage")
    return ready


async def flush_outputs(
    *,
    reason: str = "manual",
    discover_untracked: bool = True,
    requested_paths: Optional[set[str]] = None,
    skip_if_running: bool = False,
    warn_if_no_session: bool = True,
) -> set[str]:
    """Flush pending sync files to storage and return paths confirmed in-storage.

    Args:
        reason:            Label for log messages (e.g. "periodic", "present-files").
        discover_untracked: Walk filesystem to find files not in pending_sync_files.
        requested_paths:   If set, only sync these paths (used by present-files
                           to avoid a full global flush).
        skip_if_running:   Return immediately if another flush holds the lock
                           (used by periodic loop to avoid piling up).
        warn_if_no_session: Log a warning if no storage prefix is configured.
    """
    if not _has_active_sync_session():
        if warn_if_no_session:
            logger.warning(f"[flush:{reason}] No storage prefix configured, skipping sync")
        return set()

    flush_lock = _get_flush_lock()
    if skip_if_running and flush_lock.locked():
        logger.info(f"[flush:{reason}] Skipping cycle; flush already in progress")
        return set()

    async with flush_lock:
        storage_prefix = main.current_session.get("storage_prefix")
        if not storage_prefix:
            if warn_if_no_session:
                logger.warning(f"[flush:{reason}] No storage prefix configured, skipping sync")
            return set()

        if discover_untracked:
            await _discover_untracked_sync_files()
        _scrub_disallowed_pending_sync()

        if requested_paths is not None:
            candidate_paths = set()
            for virtual_path in requested_paths:
                if _queue_virtual_path_for_sync(virtual_path):
                    candidate_paths.add(virtual_path)
            if not candidate_paths:
                return set()
        else:
            candidate_paths = set(main.pending_sync_files)

        return await _sync_candidate_paths(candidate_paths, storage_prefix, reason=reason)


async def _periodic_sync_loop():
    """Background loop that continuously persists workspace files to storage.

    Every SYNC_FLUSH_INTERVAL_SEC (30s): flush files already in pending_sync_files.
    Every SYNC_RECONCILE_INTERVAL_SEC (120s): also walk the filesystem to discover
    files created by bash commands (which bypass sidecar API tracking).
    Every SYNC_FLUSH_INTERVAL_SEC (30s): also idle-reap the persistent Python
    and Node interpreters, the browser process, and any live LSP servers
    (see _reap_idle_interpreter/_reap_idle_node_interpreter/
    _reap_idle_browser/_reap_idle_lsp_servers and
    INTERPRETER_IDLE_TIMEOUT_SECONDS/NODE_INTERPRETER_IDLE_TIMEOUT_SECONDS/
    BROWSER_IDLE_TIMEOUT_SECONDS/LSP_IDLE_TIMEOUT_SECONDS).

    Uses skip_if_running=True so periodic flushes don't pile up behind a
    long-running present-files or /flush call.
    """
    while True:
        try:
            await asyncio.sleep(main.SYNC_FLUSH_INTERVAL_SEC)

            # Idle-reap the persistent interpreter on the same cadence,
            # independent of whether a storage sync session is active --
            # the interpreter's memory should be reclaimed even for a
            # session with no configured storage_prefix.
            await main._reap_idle_interpreter()
            await main._reap_idle_node_interpreter()
            await main._reap_idle_browser()
            await main._reap_idle_lsp_servers()

            if not _has_active_sync_session():
                continue

            now = _time.monotonic()
            should_reconcile = (now - main._last_reconcile_at) >= main.SYNC_RECONCILE_INTERVAL_SEC
            await main.flush_outputs(
                reason="periodic",
                discover_untracked=should_reconcile,
                skip_if_running=True,
                warn_if_no_session=False,
            )
            if should_reconcile:
                main._last_reconcile_at = now
        except asyncio.CancelledError:
            logger.info("[sync] Periodic sync loop stopped")
            raise
        except Exception as e:
            logger.error(f"[sync] Periodic loop error: {e}", exc_info=True)


async def _prefetch_namespace_from_prefix(
    storage_prefix: str,
    namespace: str,
    local_root: str,
    *,
    sandbox_owned: bool,
    chown_root: bool = True,
) -> list[str]:
    """Pre-fetch one namespace from storage into a local root path."""
    prefetched: list[str] = []
    namespace_prefix = f"{storage_prefix}/{namespace}/"
    root_real = os.path.realpath(local_root)

    try:
        keys = await main.storage().list_objects(namespace_prefix)

        for key in keys:
            rel_path = key[len(namespace_prefix):]
            if not rel_path:
                continue

            local_path = os.path.join(local_root, rel_path)
            dir_path = os.path.dirname(local_path)
            if dir_path:
                os.makedirs(dir_path, exist_ok=True)
                if sandbox_owned:
                    # chown the entire directory chain up to local_root
                    # so intermediate dirs created by makedirs aren't left
                    # as root-owned (which breaks tools like LibreOffice).
                    d = os.path.realpath(dir_path)
                    while d != root_real and d.startswith(root_real):
                        os.chown(d, main.SANDBOX_UID, main.SANDBOX_GID)
                        d = os.path.dirname(d)
                    if chown_root:
                        os.chown(root_real, main.SANDBOX_UID, main.SANDBOX_GID)

            if await main.storage().download(key, local_path):
                if sandbox_owned:
                    os.chown(local_path, main.SANDBOX_UID, main.SANDBOX_GID)
                virtual_path = f"{local_root.rstrip('/')}/{rel_path}"
                # Mark prefetched files as already-synced baseline to avoid
                # immediate re-uploads on the next discovery scan.
                sig = _file_signature(local_path)
                if sig is not None:
                    main.synced_file_signatures[virtual_path] = sig
                prefetched.append(f"{namespace}/{rel_path}")
                logger.debug(f"[prefetch] Downloaded {namespace} file: {rel_path}")

    except Exception as e:
        logger.error(f"[prefetch-{namespace}] Error: {e}", exc_info=True)

    return prefetched


async def prefetch_files(storage_prefix: str) -> list[str]:
    """
    Pre-fetch existing files from storage for this work item.

    Storage structure (unified):
        {storage_prefix}/
            uploads/{path}        - User uploads (read-only in sandbox)
            workspace/{path}      - Agent workspace files
            outputs/{path}        - Agent deliverables

    Local structure:
        /mnt/user-data/uploads/{path}
        /workspace/{path}
        /mnt/user-data/outputs/{path}
    """
    prefetched: list[str] = []

    prefetched.extend(
        await _prefetch_namespace_from_prefix(
            storage_prefix,
            "uploads",
            main.UPLOADS_DIR,
            sandbox_owned=False,
        )
    )
    prefetched.extend(
        await _prefetch_namespace_from_prefix(
            storage_prefix,
            "workspace",
            main.WORKSPACE_DIR,
            sandbox_owned=True,
        )
    )
    prefetched.extend(
        await _prefetch_namespace_from_prefix(
            storage_prefix,
            "outputs",
            main.OUTPUTS_DIR,
            sandbox_owned=True,
        )
    )

    logger.info(f"[prefetch] Pre-fetched {len(prefetched)} files")
    return prefetched


async def prefetch_uploads_from_prefix(storage_prefix: str) -> list[str]:
    """Pre-fetch uploads from unified storage prefix into /mnt/user-data/uploads/."""
    prefetched = await _prefetch_namespace_from_prefix(
        storage_prefix,
        "uploads",
        main.UPLOADS_DIR,
        sandbox_owned=False,
    )
    logger.info(f"[prefetch-uploads] Pre-fetched {len(prefetched)} uploads from storage_prefix")
    return prefetched


async def prefetch_legacy_uploads(
    organization_id: str,
    upload_file_ids: list[str],
) -> list[str]:
    """
    Pre-fetch legacy uploaded files by file ID.

    Legacy storage structure:
        uploads/{organization_id}/{file_id}/{filename}

    Local structure:
        /mnt/user-data/uploads/{filename}
    """
    prefetched = []
    queued_for_sync = 0

    try:
        for file_id in upload_file_ids:
            file_id = str(file_id).strip()
            if not file_id:
                continue

            legacy_prefix = f"uploads/{organization_id}/{file_id}/"
            legacy_keys = await main.storage().list_objects(legacy_prefix)

            for key in legacy_keys:
                rel_path = key[len(legacy_prefix):]
                if not rel_path:
                    continue

                local_path = os.path.join(main.UPLOADS_DIR, rel_path)
                dir_path = os.path.dirname(local_path)
                if dir_path:
                    os.makedirs(dir_path, exist_ok=True)

                if await main.storage().download(key, local_path):
                    prefetched.append(f"uploads/{rel_path}")
                    logger.debug(f"[prefetch-legacy] Downloaded upload: {rel_path}")

                    # Promote legacy-prefetched uploads into unified upload namespace.
                    try:
                        virtual_path = main._to_virtual_path(local_path)
                        if main._storage_bucket_for_virtual_path(virtual_path) is not None:
                            main.pending_sync_files.add(virtual_path)
                            queued_for_sync += 1
                    except HTTPException:
                        logger.warning(f"[prefetch-legacy] Skipping unsyncable path: {local_path}")

        # Persist promoted legacy uploads immediately so they survive pod recovery.
        if queued_for_sync > 0 and main.current_session.get("storage_prefix"):
            await main.flush_outputs(reason="prefetch-legacy", discover_untracked=False)
    except Exception as e:
        logger.error(f"[prefetch-legacy] Error: {e}", exc_info=True)

    logger.info(
        f"[prefetch-legacy] Pre-fetched {len(prefetched)} legacy uploads "
        f"({queued_for_sync} queued for unified sync)"
    )
    return prefetched
