"""
Optional hook for mirroring sandbox file operations into an external system.

The sandbox is fully self-contained without any of this: `bash_tool`,
`file_create`, `view`, `str_replace`, and `present_files` all work against the
sidecar's own object storage (S3 or Azure Blob) with zero external
dependencies. Nothing in this module is required to use boxkite.

`AuditSink` exists for callers who *also* want sandbox file writes mirrored
into their own system of record — a database-backed file browser, a UI that
lists "files this agent created" without querying a raw storage bucket, an
audit/compliance log, a webhook to a task-tracking system, and so on.
Implement only the methods you need; every call site treats a raised
exception here as non-fatal (logs a warning and continues) so a broken audit
integration can never fail the underlying sandbox operation.

Usage:

    class MyAuditSink:
        async def record_file_write(self, **kwargs) -> None:
            await my_db.insert_file_record(**kwargs)

        async def record_file_registered(self, **kwargs) -> None:
            await my_db.insert_file_record(**kwargs)

        async def record_exec(self, **kwargs) -> None:
            await my_db.insert_exec_record(**kwargs)

        async def get_download_url(self, **kwargs) -> str | None:
            return await my_storage.presign(kwargs["storage_key"])

    tools = create_sandbox_tools(
        sandbox_manager=manager,
        audit_sink=MyAuditSink(),
        ...
    )

Pass nothing and boxkite defaults to `NoOpAuditSink`, which records nothing
and always falls back to the sidecar's own storage-key-based reporting.

`SQLiteAuditSink` below is a ready-to-use reference implementation backed by
a local SQLite file, for callers who want a working durable audit trail
without writing their own `AuditSink`. It is unrelated to, and does not
implement, the hosted control-plane's own internal `ExecLogEntry` table
(`control_plane.models_orm.ExecLogEntry`, written by
`routers/sandboxes.py`'s `_log_exec_entry` on every exec/file-op route,
including human-takeover sessions) — that table already gives control-plane
deployments a durable, Postgres-backed audit trail with no `AuditSink`
configuration needed. `SQLiteAuditSink` exists for the self-hosted `boxkite`
package (no control-plane involved at all), where previously the only option
was `NoOpAuditSink` or a hand-rolled implementation. The two do not share
storage or a schema; don't assume parity between them.

`HashChainedSQLiteAuditSink` below is a tamper-evident extension of
`SQLiteAuditSink` (GitHub issue #136, `docs/TAMPER-EVIDENT-AUDIT-DESIGN.md`):
each row's hash covers the previous row's hash plus its own content, scoped
per `session_id`, so an ordinary `UPDATE`/`DELETE` against the table (bypassing
this sink entirely) is detectable by `verify_chain`/`verify_audit_db_file` at
the exact row it happened. `AuditSink`/`NoOpAuditSink`/`SQLiteAuditSink` are
all unchanged -- this is purely additive, per the design doc's §9
compatibility summary. The control-plane's `exec_log_entries` table gets the
analogous extension in `control_plane.repository.ExecLogEntryRepository`/
`control_plane.audit_chain`, reusing the exact same hash formula
(`compute_row_hash`/`GENESIS_HASH`/`verify_chain_rows` below) so both audit
surfaces are provably running the same algorithm.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional, Protocol, Sequence, Union, runtime_checkable
from uuid import UUID

logger = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64
"""Fixed, documented `prev_hash` for the first row of any per-session chain
(docs/TAMPER-EVIDENT-AUDIT-DESIGN.md §4). An explicit genesis constant --
rather than `NULL` -- lets a verifier treat "first row" as a normal case
instead of a special-cased branch, and lets an auditor who has never seen
the database independently compute what an empty/untampered chain's first
hash should be, from this constant alone."""


@runtime_checkable
class AuditSink(Protocol):
    """Optional external-system hook for sandbox file operations.

    Implementations should treat every method as fire-and-forget from the
    caller's perspective: `safe_call` below already catches and logs
    exceptions so a broken sink cannot break `file_create`/`str_replace`/
    `present_files`. You do not need to implement every method — boxkite
    only calls the ones relevant to the tool being used.
    """

    async def record_file_write(
        self,
        *,
        organization_id: Optional[UUID],
        work_item_id: Optional[UUID],
        session_id: Optional[str],
        agent_name: Optional[str],
        file_path: str,
        content: bytes,
    ) -> None:
        """Called after `file_create`/`str_replace` successfully writes a file."""
        ...

    async def record_file_registered(
        self,
        *,
        organization_id: Optional[UUID],
        work_item_id: Optional[UUID],
        session_id: Optional[str],
        agent_name: Optional[str],
        file_path: str,
        storage_key: str,
        size_bytes: int,
    ) -> None:
        """Called after `present_files` confirms a file is synced to storage."""
        ...

    async def record_exec(
        self,
        *,
        organization_id: Optional[UUID],
        work_item_id: Optional[UUID],
        session_id: Optional[str],
        agent_name: Optional[str],
        command: str,
        exit_code: int,
        duration_ms: int,
    ) -> None:
        """Called after `bash_tool` executes a command in the sandbox."""
        ...

    async def get_download_url(
        self,
        *,
        organization_id: Optional[UUID],
        work_item_id: Optional[UUID],
        file_path: str,
        storage_key: str,
        expiry_seconds: int,
    ) -> Optional[str]:
        """Return a download URL for a presented file.

        Return None to fall back to the sidecar's plain storage-key report
        (no signed URL) — `present_files` handles that gracefully.
        """
        ...


class NoOpAuditSink:
    """Default AuditSink: records nothing, always defers to sidecar storage."""

    async def record_file_write(self, **_kwargs: Any) -> None:
        return None

    async def record_file_registered(self, **_kwargs: Any) -> None:
        return None

    async def record_exec(self, **_kwargs: Any) -> None:
        return None

    async def get_download_url(self, **_kwargs: Any) -> Optional[str]:
        return None


class AuditEntry:
    """One row read back from `SQLiteAuditSink`'s query/replay methods."""

    __slots__ = (
        "id",
        "kind",
        "organization_id",
        "work_item_id",
        "session_id",
        "agent_name",
        "detail",
        "recorded_at",
    )

    def __init__(
        self,
        id: int,
        kind: str,
        organization_id: Optional[str],
        work_item_id: Optional[str],
        session_id: Optional[str],
        agent_name: Optional[str],
        detail: dict,
        recorded_at: str,
    ) -> None:
        self.id = id
        self.kind = kind
        self.organization_id = organization_id
        self.work_item_id = work_item_id
        self.session_id = session_id
        self.agent_name = agent_name
        self.detail = detail
        self.recorded_at = recorded_at

    def __repr__(self) -> str:  # pragma: no cover - debugging aid only
        return f"AuditEntry(id={self.id}, kind={self.kind!r}, session_id={self.session_id!r})"


def compute_row_hash(prev_hash: str, canonical_fields: dict) -> str:
    """`row_hash = sha256(prev_hash + "|" + canonical_content)`, per
    docs/TAMPER-EVIDENT-AUDIT-DESIGN.md §4. `canonical_fields` must already
    contain only JSON-safe values (str/int/float/bool/None/dict/list) --
    callers are responsible for pre-serializing anything else (e.g. a
    `datetime` to its `.isoformat()`) so the hash is reproducible across
    Python versions/platforms, since verification may run years later on a
    different machine than the one that wrote the row.
    """
    content = json.dumps(canonical_fields, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{prev_hash}|{content}".encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class ChainRow:
    """One row's chain-relevant fields, engine-agnostic -- shared between
    `SQLiteAuditSink`'s `audit_entries` table and the control-plane's
    `exec_log_entries` table so both surfaces run through the exact same
    verifier (`verify_chain_rows` below). `row_id` is whatever primary key
    type the row's own table uses (`int` for SQLite, a UUID `str` for
    `exec_log_entries`) -- it is never itself hashed except as the `"id"`
    canonical field, and is only used here to name where a break occurred.
    """

    row_id: Any
    session_id: Optional[str]
    row_hash: Optional[str]
    prev_hash: Optional[str]
    canonical_fields: dict


@dataclass(frozen=True)
class ChainVerificationResult:
    ok: bool
    rows_checked: int
    first_break_at_row_id: Optional[Any]
    detail: str


def verify_chain_rows(rows: Sequence[ChainRow]) -> ChainVerificationResult:
    """Recompute a hash chain over `rows` (already ordered by `id` within
    each session) and compare against each row's stored `row_hash`/
    `prev_hash`. Rows are grouped by `session_id` and verified independently
    (docs/TAMPER-EVIDENT-AUDIT-DESIGN.md §5 -- chain scope is per-session,
    never global), in the order each session first appears in `rows`.

    Rows with `row_hash is None` (written before hash-chaining was enabled
    for that sink/table) are skipped rather than treated as a break --
    "chain coverage begins at the first row with a non-NULL row_hash" is a
    documented boundary, not a false promise of full historical coverage
    (design doc §8). The first chained row in a session is expected to have
    `prev_hash == GENESIS_HASH` regardless of how many unhashed legacy rows
    preceded it, matching how the write path looks up "the last *chained*
    row's hash" rather than "the immediately preceding row's hash".

    Stops at the first row that fails either check (a stored `row_hash`
    that doesn't match a fresh recomputation from that row's own content --
    tampering; or a stored `prev_hash` that doesn't match the prior chained
    row's hash -- a deleted/reordered row) and reports that row's id, so a
    single corrupted row doesn't cascade into reporting every subsequent
    row as broken too.
    """
    order: list[Optional[str]] = []
    grouped: dict[Optional[str], list[ChainRow]] = {}
    for row in rows:
        if row.session_id not in grouped:
            grouped[row.session_id] = []
            order.append(row.session_id)
        grouped[row.session_id].append(row)

    total_checked = 0
    for session_key in order:
        expected_prev = GENESIS_HASH
        for row in grouped[session_key]:
            if row.row_hash is None:
                continue
            total_checked += 1
            if row.prev_hash != expected_prev:
                return ChainVerificationResult(
                    ok=False,
                    rows_checked=total_checked,
                    first_break_at_row_id=row.row_id,
                    detail=(
                        f"prev_hash mismatch at row {row.row_id!r}: expected {expected_prev}, "
                        f"found {row.prev_hash!r} -- chain broken (row deleted, reordered, or "
                        "its prev_hash was tampered with)"
                    ),
                )
            recomputed = compute_row_hash(expected_prev, row.canonical_fields)
            if recomputed != row.row_hash:
                return ChainVerificationResult(
                    ok=False,
                    rows_checked=total_checked,
                    first_break_at_row_id=row.row_id,
                    detail=(
                        f"row_hash mismatch at row {row.row_id!r}: stored hash does not match a "
                        "hash recomputed from this row's own content -- content was tampered "
                        "with after being written"
                    ),
                )
            expected_prev = row.row_hash

    last_chained = next((row for row in reversed(rows) if row.row_hash is not None), None)
    head_hash = last_chained.row_hash if last_chained is not None else GENESIS_HASH
    head_row_id = last_chained.row_id if last_chained is not None else None
    return ChainVerificationResult(
        ok=True,
        rows_checked=total_checked,
        first_break_at_row_id=None,
        detail=(
            f"chain intact; genesis={GENESIS_HASH}; head_row_id={head_row_id!r}; "
            f"head_hash={head_hash}"
        ),
    )


@runtime_checkable
class HashChainedAuditSink(AuditSink, Protocol):
    """`AuditSink` extended with chain verification. A sink implementing
    this (in addition to the base `AuditSink` methods) computes and stores
    a hash chain internally on every `record_*` write; this protocol only
    adds the read-side verification contract, since the write side stays
    inside each `record_*` method's existing signature -- no new required
    write method, no change to any existing method's kwargs.
    """

    async def verify_chain(self, *, session_id: Optional[str] = None) -> ChainVerificationResult:
        """Recompute the hash chain over stored rows and compare against
        the stored `row_hash`/`prev_hash` values. `session_id=None` verifies
        the sink's entire chain (every session, each independently);
        otherwise scoped to one session.
        """
        ...


class SQLiteAuditSink:
    """SQLite-backed reference implementation of `AuditSink`.

    Gives self-hosted users of the bare `boxkite` package a working, durable
    audit trail out of the box, instead of only the `AuditSink` interface.
    Every method is implemented, including `get_download_url`, which always
    returns `None` — this sink has no object storage of its own, so
    `present_files` correctly falls back to the sidecar's plain storage-key
    report for downloads; only the audit record is kept locally.

    Not a substitute for a multi-replica/multi-process deployment's shared
    store: a local SQLite file is invisible to other processes. Use this for
    a standalone `boxkite` deployment, or replace it with your own
    `AuditSink` backed by Postgres/Redis/etc. for a distributed one.
    """

    def __init__(self, db_path: Union[str, Path] = "boxkite_audit.db") -> None:
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
                CREATE TABLE IF NOT EXISTS audit_entries (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    organization_id TEXT,
                    work_item_id TEXT,
                    session_id TEXT,
                    agent_name TEXT,
                    detail TEXT NOT NULL,
                    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS ix_audit_entries_session "
                "ON audit_entries(session_id, id)"
            )

    def _insert(self, kind: str, *, organization_id, work_item_id, session_id, agent_name, detail: dict) -> None:
        def _write() -> None:
            with self._lock, self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO audit_entries
                        (kind, organization_id, work_item_id, session_id, agent_name, detail)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        kind,
                        str(organization_id) if organization_id else None,
                        str(work_item_id) if work_item_id else None,
                        session_id,
                        agent_name,
                        json.dumps(detail),
                    ),
                )

        return _write()

    async def record_file_write(
        self,
        *,
        organization_id: Optional[UUID],
        work_item_id: Optional[UUID],
        session_id: Optional[str],
        agent_name: Optional[str],
        file_path: str,
        content: bytes,
    ) -> None:
        await asyncio.to_thread(
            self._insert,
            "file_write",
            organization_id=organization_id,
            work_item_id=work_item_id,
            session_id=session_id,
            agent_name=agent_name,
            detail={"file_path": file_path, "size_bytes": len(content)},
        )

    async def record_file_registered(
        self,
        *,
        organization_id: Optional[UUID],
        work_item_id: Optional[UUID],
        session_id: Optional[str],
        agent_name: Optional[str],
        file_path: str,
        storage_key: str,
        size_bytes: int,
    ) -> None:
        await asyncio.to_thread(
            self._insert,
            "file_registered",
            organization_id=organization_id,
            work_item_id=work_item_id,
            session_id=session_id,
            agent_name=agent_name,
            detail={
                "file_path": file_path,
                "storage_key": storage_key,
                "size_bytes": size_bytes,
            },
        )

    async def record_exec(
        self,
        *,
        organization_id: Optional[UUID],
        work_item_id: Optional[UUID],
        session_id: Optional[str],
        agent_name: Optional[str],
        command: str,
        exit_code: int,
        duration_ms: int,
    ) -> None:
        await asyncio.to_thread(
            self._insert,
            "exec",
            organization_id=organization_id,
            work_item_id=work_item_id,
            session_id=session_id,
            agent_name=agent_name,
            detail={
                "command": command,
                "exit_code": exit_code,
                "duration_ms": duration_ms,
            },
        )

    async def get_download_url(self, **_kwargs: Any) -> Optional[str]:
        return None

    async def list_for_session(self, session_id: str) -> List[AuditEntry]:
        """Replay a session's full audit trail in the order it was recorded."""

        def _read() -> list:
            with self._connect() as conn:
                cursor = conn.execute(
                    """
                    SELECT id, kind, organization_id, work_item_id, session_id,
                           agent_name, detail, recorded_at
                    FROM audit_entries
                    WHERE session_id = ?
                    ORDER BY id
                    """,
                    (session_id,),
                )
                return cursor.fetchall()

        rows = await asyncio.to_thread(_read)
        return [
            AuditEntry(
                id=row[0],
                kind=row[1],
                organization_id=row[2],
                work_item_id=row[3],
                session_id=row[4],
                agent_name=row[5],
                detail=json.loads(row[6]),
                recorded_at=row[7],
            )
            for row in rows
        ]


class HashChainedSQLiteAuditSink(SQLiteAuditSink):
    """`SQLiteAuditSink` subclass that additionally computes and persists a
    hash chain on every write (docs/TAMPER-EVIDENT-AUDIT-DESIGN.md). A
    subclass, not a fork -- reuses `SQLiteAuditSink`'s `_connect`/lock/
    `list_for_session` machinery unchanged and only overrides `_init_db`
    (to add two nullable columns) and `_insert` (to compute and persist
    `row_hash`/`prev_hash` in the same `INSERT` as the row itself, never a
    follow-up `UPDATE`).

    Existing `SQLiteAuditSink` databases work unmodified: `_init_db` adds
    `row_hash`/`prev_hash` as nullable columns if they're missing, and any
    pre-existing rows keep `NULL` in both -- "chain coverage begins at the
    first row with a non-NULL row_hash" (design doc §8), not a promise of
    full historical coverage.
    """

    def _init_db(self) -> None:
        super()._init_db()
        with self._lock, self._connect() as conn:
            existing_columns = {row[1] for row in conn.execute("PRAGMA table_info(audit_entries)")}
            if "row_hash" not in existing_columns:
                conn.execute("ALTER TABLE audit_entries ADD COLUMN row_hash TEXT")
            if "prev_hash" not in existing_columns:
                conn.execute("ALTER TABLE audit_entries ADD COLUMN prev_hash TEXT")

    def _insert(self, kind: str, *, organization_id, work_item_id, session_id, agent_name, detail: dict) -> None:
        def _write() -> None:
            with self._lock, self._connect() as conn:
                # Guarded by self._lock (single writer per sink instance) so
                # the next-id/prev-hash lookups below and the INSERT that
                # uses them are effectively one atomic step -- consistent
                # with this table's existing append-only, single-INSERT
                # write path (no separate rows are ever deleted in normal
                # operation; see SQLiteAuditSink's own docstring).
                next_id = conn.execute(
                    "SELECT COALESCE(MAX(id), 0) + 1 FROM audit_entries"
                ).fetchone()[0]
                recorded_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
                organization_id_str = str(organization_id) if organization_id else None
                work_item_id_str = str(work_item_id) if work_item_id else None

                prev_row = conn.execute(
                    "SELECT row_hash FROM audit_entries WHERE session_id IS ? AND row_hash IS NOT NULL "
                    "ORDER BY id DESC LIMIT 1",
                    (session_id,),
                ).fetchone()
                prev_hash = prev_row[0] if prev_row is not None else GENESIS_HASH

                canonical_fields = {
                    "id": next_id,
                    "kind": kind,
                    "organization_id": organization_id_str,
                    "work_item_id": work_item_id_str,
                    "session_id": session_id,
                    "agent_name": agent_name,
                    "detail": detail,
                    "recorded_at": recorded_at,
                }
                row_hash = compute_row_hash(prev_hash, canonical_fields)

                conn.execute(
                    """
                    INSERT INTO audit_entries
                        (id, kind, organization_id, work_item_id, session_id, agent_name,
                         detail, recorded_at, row_hash, prev_hash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        next_id,
                        kind,
                        organization_id_str,
                        work_item_id_str,
                        session_id,
                        agent_name,
                        json.dumps(detail),
                        recorded_at,
                        row_hash,
                        prev_hash,
                    ),
                )

        return _write()

    async def verify_chain(self, *, session_id: Optional[str] = None) -> ChainVerificationResult:
        def _read() -> list:
            with self._connect() as conn:
                if session_id is not None:
                    cursor = conn.execute(
                        _CHAIN_SELECT_COLUMNS + " FROM audit_entries WHERE session_id = ? ORDER BY id",
                        (session_id,),
                    )
                else:
                    cursor = conn.execute(
                        _CHAIN_SELECT_COLUMNS + " FROM audit_entries ORDER BY session_id, id"
                    )
                return cursor.fetchall()

        rows = await asyncio.to_thread(_read)
        return verify_chain_rows([_sqlite_row_to_chain_row(row) for row in rows])


_CHAIN_SELECT_COLUMNS = (
    "SELECT id, session_id, row_hash, prev_hash, kind, organization_id, "
    "work_item_id, agent_name, detail, recorded_at"
)


def _sqlite_row_to_chain_row(row: tuple) -> ChainRow:
    (row_id, session_id, row_hash, prev_hash, kind, organization_id, work_item_id, agent_name, detail, recorded_at) = row
    return ChainRow(
        row_id=row_id,
        session_id=session_id,
        row_hash=row_hash,
        prev_hash=prev_hash,
        canonical_fields={
            "id": row_id,
            "kind": kind,
            "organization_id": organization_id,
            "work_item_id": work_item_id,
            "session_id": session_id,
            "agent_name": agent_name,
            "detail": json.loads(detail),
            "recorded_at": recorded_at,
        },
    )


def verify_audit_db_file(
    db_path: Union[str, Path], *, session_id: Optional[str] = None
) -> ChainVerificationResult:
    """Read-only verifier for a `HashChainedSQLiteAuditSink`-backed database
    file -- the artifact an external auditor should actually run
    (docs/TAMPER-EVIDENT-AUDIT-DESIGN.md §7), independent of any live sink
    instance or process. Opens the file in SQLite's `mode=ro` URI mode, so
    this function can never itself mutate the audit log even if called
    against the live file rather than an exported copy.

    Synchronous by design (a CLI-oriented verifier, not an async hot path).
    """
    uri = f"file:{Path(db_path).resolve()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        if session_id is not None:
            rows = conn.execute(
                _CHAIN_SELECT_COLUMNS + " FROM audit_entries WHERE session_id = ? ORDER BY id",
                (session_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                _CHAIN_SELECT_COLUMNS + " FROM audit_entries ORDER BY session_id, id"
            ).fetchall()
    finally:
        conn.close()
    return verify_chain_rows([_sqlite_row_to_chain_row(row) for row in rows])


async def safe_call(sink: Optional[Any], method_name: str, **kwargs: Any) -> Any:
    """Call an AuditSink method defensively, logging instead of raising.

    Missing methods (a sink that only implements a subset of AuditSink) are
    treated the same as a no-op — this makes partial implementations safe.
    """
    if sink is None:
        return None
    method = getattr(sink, method_name, None)
    if method is None:
        return None
    try:
        return await method(**kwargs)
    except Exception as exc:  # noqa: BLE001 - audit hooks must never break tools
        logger.warning("[AuditSink] %s failed: %s", method_name, exc)
        return None
