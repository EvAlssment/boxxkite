"""
Optional hook for reconstructing session ownership after pod loss.

`SandboxManager` stores session ownership (organization_id, work_item_id,
upload_file_ids, storage_prefix) as labels/annotations on the K8s pod itself —
so recovery after a transient sidecar transport error normally just re-reads
the pod. But if the pod is already gone (deleted out-of-band, or killed by
the `SANDBOX_ACTIVE_DEADLINE_SECONDS` backstop) there is nothing left in K8s
to read.

`SessionMetadataStore` is the optional escape hatch for that case: implement
`reconstruct()` against whatever durable store you use to track sessions (a
database, Redis, an in-memory dict backed by your own session manager, etc.)
and pass an instance to `SandboxManager(session_metadata_store=...)`.

Callers who don't need recovery for already-deleted pods can leave this
unset — `SandboxManager` defaults to `NoOpSessionMetadataStore`, which simply
means that specific recovery path raises "session not found" instead of
reconstructing state. This is the same behavior the sandbox has with no store
configured at all; nothing about normal operation (create/destroy/recycle,
warm-pool claim, in-flight tool calls) depends on this hook.

`SQLiteSessionMetadataStore` below is a reference implementation: pass it as
`session_metadata_store` and session recovery works out of the box, with no
custom store to write. It also implements an additional `record()` method
(outside the `SessionMetadataStore` Protocol, which only requires
`reconstruct()`) that `SandboxManager` calls, best-effort, whenever it
successfully establishes a session's ownership — feature-detected via
`hasattr`, the same pattern `AuditSink` call sites use, so stores that only
implement `reconstruct()` (e.g. one backed by a system that already records
ownership elsewhere) are unaffected.

A local SQLite file is single-process: it is a good fit for a standalone
`boxkite` deployment or a single-replica control-plane, but is invisible to
other replicas in a multi-replica control-plane deployment. Back
`SessionMetadataStore` with your existing shared Postgres/Redis store instead
if you run more than one control-plane replica.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Protocol, Union, runtime_checkable
from uuid import UUID


@dataclass(frozen=True)
class SessionMetadata:
    """Reconstructed session ownership, enough to recreate a pod correctly."""

    organization_id: Optional[UUID]
    work_item_id: Optional[UUID]
    storage_prefix: str
    upload_file_ids: List[str] = field(default_factory=list)


@runtime_checkable
class SessionMetadataStore(Protocol):
    async def reconstruct(self, session_id: str) -> Optional[SessionMetadata]:
        """Best-effort reconstruction of session ownership after pod loss.

        Return None if the session is unknown to your store. SandboxManager
        treats that identically to "no store configured" — recovery fails
        with a clear "session not found" error rather than silently
        recreating a pod with no owner.
        """
        ...


class NoOpSessionMetadataStore:
    """Default SessionMetadataStore: never reconstructs, always returns None."""

    async def reconstruct(self, session_id: str) -> Optional[SessionMetadata]:
        return None


class SQLiteSessionMetadataStore:
    """SQLite-backed reference implementation of `SessionMetadataStore`.

    Satisfies `reconstruct()` for recovery after pod loss, and additionally
    exposes `record()`/`forget()` so `SandboxManager` can keep this store
    current on its own — see module docstring.
    """

    def __init__(self, db_path: Union[str, Path] = "boxkite_sessions.db") -> None:
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._lock, self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS session_metadata (
                    session_id TEXT PRIMARY KEY,
                    organization_id TEXT,
                    work_item_id TEXT,
                    storage_prefix TEXT NOT NULL,
                    upload_file_ids TEXT NOT NULL,
                    updated_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )

    async def record(
        self,
        session_id: str,
        *,
        organization_id: Optional[UUID],
        work_item_id: Optional[UUID],
        storage_prefix: str,
        upload_file_ids: Optional[List[str]] = None,
    ) -> None:
        """Persist (or overwrite) session ownership so `reconstruct` can find it later."""

        def _write() -> None:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO session_metadata
                        (session_id, organization_id, work_item_id, storage_prefix, upload_file_ids, updated_at)
                    VALUES (?, ?, ?, ?, ?, datetime('now'))
                    ON CONFLICT(session_id) DO UPDATE SET
                        organization_id = excluded.organization_id,
                        work_item_id = excluded.work_item_id,
                        storage_prefix = excluded.storage_prefix,
                        upload_file_ids = excluded.upload_file_ids,
                        updated_at = excluded.updated_at
                    """,
                    (
                        session_id,
                        str(organization_id) if organization_id else None,
                        str(work_item_id) if work_item_id else None,
                        storage_prefix,
                        json.dumps(upload_file_ids or []),
                    ),
                )

        await asyncio.to_thread(_write)

    async def reconstruct(self, session_id: str) -> Optional[SessionMetadata]:
        def _read() -> Optional[tuple]:
            with self._connect() as conn:
                cursor = conn.execute(
                    "SELECT organization_id, work_item_id, storage_prefix, upload_file_ids "
                    "FROM session_metadata WHERE session_id = ?",
                    (session_id,),
                )
                return cursor.fetchone()

        row = await asyncio.to_thread(_read)
        if row is None:
            return None
        organization_id, work_item_id, storage_prefix, upload_file_ids_json = row
        return SessionMetadata(
            organization_id=UUID(organization_id) if organization_id else None,
            work_item_id=UUID(work_item_id) if work_item_id else None,
            storage_prefix=storage_prefix,
            upload_file_ids=json.loads(upload_file_ids_json),
        )

    async def forget(self, session_id: str) -> None:
        """Remove a session's stored metadata, e.g. after a clean teardown."""

        def _delete() -> None:
            with self._lock, self._connect() as conn:
                conn.execute(
                    "DELETE FROM session_metadata WHERE session_id = ?", (session_id,)
                )

        await asyncio.to_thread(_delete)
