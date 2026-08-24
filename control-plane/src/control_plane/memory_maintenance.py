"""Explicit, bounded maintenance primitives for the account-scoped MemoryBase.

This module intentionally has no scheduler or application startup hook.  A
caller owns the cadence and supplies one account at a time.  Every statement
issued here includes ``account_id`` so a maintenance invocation cannot cross a
tenant boundary even if it is handed an id from another account.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import set_committed_value

from .memory_embeddings import MemoryVectorStore
from .memory_engine import jaccard_similarity, relation_type
from .models_orm import MemoryEmbedding, MemoryRecord, MemoryRelation

DEFAULT_BATCH_SIZE = 100
MAX_BATCH_SIZE = 500
DEFAULT_MAX_BATCHES = 10
MAX_BATCHES = 100


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _live_clause(now: datetime):
    return and_(
        or_(MemoryRecord.expires_at.is_(None), MemoryRecord.expires_at > now),
        MemoryRecord.superseded_at.is_(None),
    )


def _cursor_clause(after: tuple[datetime, str] | None):
    if after is None:
        return None
    created_at, row_id = after
    return or_(
        MemoryRecord.created_at > created_at,
        and_(MemoryRecord.created_at == created_at, MemoryRecord.id > row_id),
    )


def _validate_bounds(batch_size: int, max_batches: int) -> None:
    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")
    if not 1 <= max_batches <= MAX_BATCHES:
        raise ValueError(f"max_batches must be between 1 and {MAX_BATCHES}")


@dataclass(slots=True)
class MaintenanceStats:
    """Structured result shared by every maintenance primitive.

    In dry-run mode, ``deleted`` and ``merged`` are planned changes rather
    than writes.  ``has_more`` tells a bounded caller that another explicit
    invocation is needed to finish the account.
    """

    operation: str
    account_id: str
    dry_run: bool
    batch_size: int
    max_batches: int
    batches: int = 0
    scanned: int = 0
    matched: int = 0
    candidates: int = 0
    deleted: int = 0
    superseded: int = 0
    merged: int = 0
    relations_added: int = 0
    relations_removed: int = 0
    relations_rewired: int = 0
    embedded: int = 0
    has_more: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "operation": self.operation,
            "account_id": self.account_id,
            "dry_run": self.dry_run,
            "batch_size": self.batch_size,
            "max_batches": self.max_batches,
            "batches": self.batches,
            "scanned": self.scanned,
            "matched": self.matched,
            "candidates": self.candidates,
            "deleted": self.deleted,
            "superseded": self.superseded,
            "merged": self.merged,
            "relations_added": self.relations_added,
            "relations_removed": self.relations_removed,
            "relations_rewired": self.relations_rewired,
            "embedded": self.embedded,
            "has_more": self.has_more,
        }


class MemoryMaintenance:
    """Operator-invoked maintenance over one account's MemoryBase rows."""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def backfill_embeddings(
        self,
        *,
        account_id: str,
        vector_store: MemoryVectorStore,
        scope: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_batches: int = DEFAULT_MAX_BATCHES,
        dry_run: bool = False,
    ) -> MaintenanceStats:
        _validate_bounds(batch_size, max_batches)
        stats = MaintenanceStats(
            operation="backfill_embeddings",
            account_id=account_id,
            dry_run=dry_run,
            batch_size=batch_size,
            max_batches=max_batches,
        )
        after: tuple[datetime, str] | None = None
        while stats.batches < max_batches:
            filters = [
                MemoryRecord.account_id == account_id,
                _live_clause(_utcnow()),
                MemoryEmbedding.id.is_(None),
            ]
            if scope is not None:
                filters.append(MemoryRecord.scope == scope)
            cursor = _cursor_clause(after)
            if cursor is not None:
                filters.append(cursor)
            result = await self.db.execute(
                select(MemoryRecord)
                .outerjoin(
                    MemoryEmbedding,
                    and_(
                        MemoryEmbedding.memory_id == MemoryRecord.id,
                        MemoryEmbedding.model_id == vector_store.model_id,
                    ),
                )
                .where(*filters)
                .order_by(MemoryRecord.created_at, MemoryRecord.id)
                .limit(batch_size)
            )
            rows = list(result.scalars().all())
            if not rows:
                break
            stats.batches += 1
            stats.scanned += len(rows)
            stats.matched += len(rows)
            stats.candidates += len(rows)
            stats.embedded += len(rows)
            after = (rows[-1].created_at, rows[-1].id)
            if not dry_run:
                await vector_store.upsert_many(
                    account_id=account_id,
                    memories=[(row.id, row.content) for row in rows],
                )
                await self.db.commit()
            if len(rows) < batch_size:
                break
            stats.has_more = True
        return stats

    async def purge_expired(
        self,
        *,
        account_id: str,
        now: datetime | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_batches: int = DEFAULT_MAX_BATCHES,
        dry_run: bool = False,
    ) -> MaintenanceStats:
        """Permanently remove expired memories and their derived edges."""

        _validate_bounds(batch_size, max_batches)
        cutoff = now or _utcnow()
        stats = MaintenanceStats(
            operation="purge_expired",
            account_id=account_id,
            dry_run=dry_run,
            batch_size=batch_size,
            max_batches=max_batches,
        )
        after: tuple[datetime, str] | None = None

        while stats.batches < max_batches:
            filters = [
                MemoryRecord.account_id == account_id,
                MemoryRecord.expires_at.is_not(None),
                MemoryRecord.expires_at <= cutoff,
            ]
            cursor = _cursor_clause(after)
            if cursor is not None:
                filters.append(cursor)
            result = await self.db.execute(
                select(MemoryRecord.id, MemoryRecord.created_at)
                .where(*filters)
                .order_by(MemoryRecord.created_at, MemoryRecord.id)
                .limit(batch_size)
            )
            rows = list(result.all())
            if not rows:
                break

            stats.batches += 1
            stats.scanned += len(rows)
            stats.matched += len(rows)
            ids = [row_id for row_id, _created_at in rows]
            after = (rows[-1][1], rows[-1][0])

            relation_count = await self._count_relations_for_memory_ids(account_id, ids)
            stats.relations_removed += relation_count
            stats.deleted += len(ids)
            if not dry_run:
                await self.db.execute(
                    delete(MemoryRelation).where(
                        MemoryRelation.account_id == account_id,
                        or_(
                            MemoryRelation.source_memory_id.in_(ids),
                            MemoryRelation.target_memory_id.in_(ids),
                        ),
                    )
                )
                await self.db.execute(
                    delete(MemoryRecord).where(
                        MemoryRecord.account_id == account_id,
                        MemoryRecord.id.in_(ids),
                    )
                )
                await self.db.commit()

            if len(rows) < batch_size:
                break
            stats.has_more = True

        return stats

    async def compact(
        self,
        *,
        account_id: str,
        scope: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_batches: int = DEFAULT_MAX_BATCHES,
        near_duplicate_threshold: float = 0.88,
        dry_run: bool = False,
    ) -> MaintenanceStats:
        """Compact exact and bounded near-duplicate live memories.

        Exact duplicates are grouped by scope and content hash.  Near
        duplicates are compared inside bounded ordered batches and require the
        same scope and kind.  The oldest row is retained; metadata, dates,
        provenance, importance, and access counters are merged into it.
        """

        _validate_bounds(batch_size, max_batches)
        if not 0.0 < near_duplicate_threshold <= 1.0:
            raise ValueError("near_duplicate_threshold must be greater than 0 and at most 1")
        stats = MaintenanceStats(
            operation="compact",
            account_id=account_id,
            dry_run=dry_run,
            batch_size=batch_size,
            max_batches=max_batches,
        )

        group_offset = 0
        while stats.batches < max_batches:
            filters = [MemoryRecord.account_id == account_id, _live_clause(_utcnow())]
            if scope is not None:
                filters.append(MemoryRecord.scope == scope)
            groups_result = await self.db.execute(
                select(
                    MemoryRecord.scope,
                    MemoryRecord.content_hash,
                    func.count().label("duplicate_count"),
                )
                .where(*filters)
                .group_by(MemoryRecord.scope, MemoryRecord.content_hash)
                .having(func.count() > 1)
                .order_by(MemoryRecord.scope, MemoryRecord.content_hash)
                .offset(group_offset if dry_run else 0)
                .limit(batch_size)
            )
            groups = list(groups_result.all())
            if not groups:
                break

            stats.batches += 1
            exact_changes = 0
            for group_scope, content_hash, _count in groups:
                rows_result = await self.db.execute(
                    select(MemoryRecord)
                    .where(
                        MemoryRecord.account_id == account_id,
                        MemoryRecord.scope == group_scope,
                        MemoryRecord.content_hash == content_hash,
                        MemoryRecord.superseded_at.is_(None),
                        or_(
                            MemoryRecord.expires_at.is_(None),
                            MemoryRecord.expires_at > _utcnow(),
                        ),
                    )
                    .order_by(MemoryRecord.created_at, MemoryRecord.id)
                    .limit(batch_size)
                )
                rows = [row for row in rows_result.scalars().all() if row.content]
                exact_groups = self._split_exact_groups(rows)
                for exact_rows in exact_groups:
                    if len(exact_rows) < 2:
                        continue
                    stats.scanned += len(exact_rows)
                    stats.matched += len(exact_rows)
                    stats.candidates += len(exact_rows) - 1
                    stats.merged += len(exact_rows) - 1
                    stats.superseded += len(exact_rows) - 1
                    if not dry_run:
                        stats.relations_rewired += await self._merge_rows(
                            account_id=account_id,
                            survivor=exact_rows[0],
                            duplicates=exact_rows[1:],
                        )
                    exact_changes += len(exact_rows) - 1

            if dry_run:
                group_offset += len(groups)
            elif exact_changes == 0:
                break

            if len(groups) < batch_size:
                if dry_run or exact_changes == 0:
                    break
            else:
                stats.has_more = True

        if stats.batches < max_batches:
            near_stats = await self._compact_near_duplicates(
                account_id=account_id,
                scope=scope,
                batch_size=batch_size,
                max_batches=max_batches - stats.batches,
                threshold=near_duplicate_threshold,
                dry_run=dry_run,
            )
            stats.batches += near_stats.batches
            stats.scanned += near_stats.scanned
            stats.matched += near_stats.matched
            stats.candidates += near_stats.candidates
            stats.deleted += near_stats.deleted
            stats.superseded += near_stats.superseded
            stats.merged += near_stats.merged
            stats.relations_rewired += near_stats.relations_rewired
            stats.has_more = stats.has_more or near_stats.has_more
        else:
            stats.has_more = True

        return stats

    async def rebuild_relations(
        self,
        *,
        account_id: str,
        scope: str | None = None,
        batch_size: int = DEFAULT_BATCH_SIZE,
        max_batches: int = DEFAULT_MAX_BATCHES,
        similarity_threshold: float = 0.45,
        dry_run: bool = False,
    ) -> MaintenanceStats:
        """Reconcile derived outgoing relations for bounded live row batches."""

        _validate_bounds(batch_size, max_batches)
        if not 0.0 < similarity_threshold <= 1.0:
            raise ValueError("similarity_threshold must be greater than 0 and at most 1")
        stats = MaintenanceStats(
            operation="rebuild_relations",
            account_id=account_id,
            dry_run=dry_run,
            batch_size=batch_size,
            max_batches=max_batches,
        )
        after: tuple[datetime, str] | None = None

        while stats.batches < max_batches:
            filters = [MemoryRecord.account_id == account_id, _live_clause(_utcnow())]
            if scope is not None:
                filters.append(MemoryRecord.scope == scope)
            cursor = _cursor_clause(after)
            if cursor is not None:
                filters.append(cursor)
            result = await self.db.execute(
                select(MemoryRecord)
                .where(*filters)
                .order_by(MemoryRecord.created_at, MemoryRecord.id)
                .limit(batch_size)
            )
            sources = list(result.scalars().all())
            if not sources:
                break

            stats.batches += 1
            stats.scanned += len(sources)
            after = (sources[-1].created_at, sources[-1].id)
            for source in sources:
                desired = await self._desired_relations(
                    account_id=account_id,
                    source=source,
                    threshold=similarity_threshold,
                    candidate_limit=batch_size,
                )
                stats.matched += len(desired)
                existing_result = await self.db.execute(
                    select(MemoryRelation)
                    .where(
                        MemoryRelation.account_id == account_id,
                        MemoryRelation.source_memory_id == source.id,
                    )
                )
                existing = list(existing_result.scalars().all())
                existing_by_key = {
                    (edge.target_memory_id, edge.relation_type): edge for edge in existing
                }
                desired_keys = set(desired)
                stale = [
                    edge
                    for key, edge in existing_by_key.items()
                    if key not in desired_keys
                ]
                missing = [
                    key for key in desired_keys if key not in existing_by_key
                ]
                stats.relations_removed += len(stale)
                stats.relations_added += len(missing)
                if not dry_run:
                    if stale:
                        await self.db.execute(
                            delete(MemoryRelation).where(
                                MemoryRelation.account_id == account_id,
                                MemoryRelation.id.in_([edge.id for edge in stale]),
                            )
                        )
                    for target_id, edge_type in missing:
                        self.db.add(
                            MemoryRelation(
                                account_id=account_id,
                                source_memory_id=source.id,
                                target_memory_id=target_id,
                                relation_type=edge_type,
                                confidence=desired[(target_id, edge_type)],
                            )
                        )

            if not dry_run:
                await self.db.commit()
            if len(sources) < batch_size:
                break
            stats.has_more = True

        return stats

    async def _compact_near_duplicates(
        self,
        *,
        account_id: str,
        scope: str | None,
        batch_size: int,
        max_batches: int,
        threshold: float,
        dry_run: bool,
    ) -> MaintenanceStats:
        stats = MaintenanceStats(
            operation="compact_near_duplicates",
            account_id=account_id,
            dry_run=dry_run,
            batch_size=batch_size,
            max_batches=max_batches,
        )
        after: tuple[datetime, str] | None = None
        while stats.batches < max_batches:
            filters = [MemoryRecord.account_id == account_id, _live_clause(_utcnow())]
            if scope is not None:
                filters.append(MemoryRecord.scope == scope)
            cursor = _cursor_clause(after)
            if cursor is not None:
                filters.append(cursor)
            result = await self.db.execute(
                select(MemoryRecord)
                .where(*filters)
                .order_by(MemoryRecord.created_at, MemoryRecord.id)
                .limit(batch_size)
            )
            rows = list(result.scalars().all())
            if not rows:
                break
            stats.batches += 1
            stats.scanned += len(rows)
            after = (rows[-1].created_at, rows[-1].id)
            representatives: list[MemoryRecord] = []
            for row in rows:
                match = next(
                    (
                        representative
                        for representative in representatives
                        if representative.scope == row.scope
                        and representative.kind == row.kind
                        and representative.content_hash != row.content_hash
                        and jaccard_similarity(representative.content, row.content) >= threshold
                    ),
                    None,
                )
                if match is None:
                    representatives.append(row)
                    continue
                stats.matched += 2
                stats.candidates += 1
                stats.merged += 1
                stats.superseded += 1
                if not dry_run:
                    stats.relations_rewired += await self._merge_rows(
                        account_id=account_id,
                        survivor=match,
                        duplicates=[row],
                    )
                else:
                    representatives.append(row)
            if len(rows) < batch_size:
                break
            stats.has_more = True
        return stats

    @staticmethod
    def _split_exact_groups(rows: list[MemoryRecord]) -> list[list[MemoryRecord]]:
        groups: dict[str, list[MemoryRecord]] = {}
        for row in rows:
            groups.setdefault(row.content, []).append(row)
        return list(groups.values())

    async def _merge_rows(
        self,
        *,
        account_id: str,
        survivor: MemoryRecord,
        duplicates: list[MemoryRecord],
    ) -> int:
        duplicate_ids = [row.id for row in duplicates if row.id != survivor.id]
        if not duplicate_ids:
            return 0
        all_rows = [survivor, *duplicates]
        metadata: dict[str, Any] = {}
        for row in all_rows:
            metadata.update(row.metadata_json or {})
        event_dates: list[str] = []
        for row in all_rows:
            for event_date in row.event_dates_json or []:
                if event_date not in event_dates:
                    event_dates.append(event_date)
        expiries = [row.expires_at for row in all_rows]
        merged_expires_at = None if any(value is None for value in expiries) else max(expiries)
        merged_source = next((row.source_content for row in reversed(all_rows) if row.source_content), None)
        merged_session = next((row.source_session_id for row in reversed(all_rows) if row.source_session_id), None)
        merged_document_date = max(
            (row.document_date for row in all_rows if row.document_date),
            default=None,
        )
        merged_last_accessed = max(
            (row.last_accessed_at for row in all_rows if row.last_accessed_at),
            default=None,
        )
        values = {
            "metadata_json": metadata or None,
            "source_content": merged_source,
            "source_session_id": merged_session,
            "document_date": merged_document_date,
            "event_dates_json": event_dates,
            "importance": max(row.importance for row in all_rows),
            "access_count": sum(row.access_count for row in all_rows),
            "last_accessed_at": merged_last_accessed,
            "expires_at": merged_expires_at,
            "updated_at": max(row.updated_at for row in all_rows),
        }
        await self.db.execute(
            update(MemoryRecord)
            .where(MemoryRecord.account_id == account_id, MemoryRecord.id == survivor.id)
            .values(**values)
        )
        for field, value in values.items():
            set_committed_value(survivor, field, value)

        relations_result = await self.db.execute(
            select(MemoryRelation)
            .where(
                MemoryRelation.account_id == account_id,
                or_(
                    MemoryRelation.source_memory_id.in_(duplicate_ids),
                    MemoryRelation.target_memory_id.in_(duplicate_ids),
                ),
            )
        )
        affected_relations = list(relations_result.scalars().all())
        existing_result = await self.db.execute(
            select(MemoryRelation.source_memory_id, MemoryRelation.target_memory_id, MemoryRelation.relation_type)
            .where(
                MemoryRelation.account_id == account_id,
                or_(
                    MemoryRelation.source_memory_id == survivor.id,
                    MemoryRelation.target_memory_id == survivor.id,
                ),
            )
        )
        existing_keys = set(existing_result.all())
        if affected_relations:
            await self.db.execute(
                delete(MemoryRelation).where(
                    MemoryRelation.account_id == account_id,
                    MemoryRelation.id.in_([edge.id for edge in affected_relations]),
                )
            )
        rewired = 0
        for edge in affected_relations:
            source_id = survivor.id if edge.source_memory_id in duplicate_ids else edge.source_memory_id
            target_id = survivor.id if edge.target_memory_id in duplicate_ids else edge.target_memory_id
            key = (source_id, target_id, edge.relation_type)
            if source_id == target_id or key in existing_keys:
                continue
            self.db.add(
                MemoryRelation(
                    account_id=account_id,
                    source_memory_id=source_id,
                    target_memory_id=target_id,
                    relation_type=edge.relation_type,
                    confidence=edge.confidence,
                )
            )
            existing_keys.add(key)
            rewired += 1

        now = _utcnow()
        await self.db.execute(
            update(MemoryRecord)
            .where(
                MemoryRecord.account_id == account_id,
                MemoryRecord.id.in_(duplicate_ids),
                MemoryRecord.superseded_at.is_(None),
            )
            .values(superseded_at=now, superseded_by_id=survivor.id)
        )
        await self.db.commit()
        return rewired

    async def _desired_relations(
        self,
        *,
        account_id: str,
        source: MemoryRecord,
        threshold: float,
        candidate_limit: int,
    ) -> dict[tuple[str, str], float]:
        result = await self.db.execute(
            select(MemoryRecord)
            .where(
                MemoryRecord.account_id == account_id,
                MemoryRecord.scope == source.scope,
                MemoryRecord.id != source.id,
                _live_clause(_utcnow()),
            )
            .order_by(MemoryRecord.updated_at.desc(), MemoryRecord.id)
            .limit(candidate_limit)
        )
        desired: dict[tuple[str, str], float] = {}
        for target in result.scalars().all():
            similarity = jaccard_similarity(source.content, target.content)
            edge_type = relation_type(f" {source.content.lower()} ", target.content)
            edge_threshold = 0.25 if edge_type == "updates" else threshold
            if similarity < edge_threshold:
                continue
            desired[(target.id, edge_type)] = round(similarity, 4)
        return desired

    async def _count_relations_for_memory_ids(self, account_id: str, ids: list[str]) -> int:
        result = await self.db.execute(
            select(func.count())
            .select_from(MemoryRelation)
            .where(
                MemoryRelation.account_id == account_id,
                or_(
                    MemoryRelation.source_memory_id.in_(ids),
                    MemoryRelation.target_memory_id.in_(ids),
                ),
            )
        )
        return int(result.scalar_one())


__all__ = ["MaintenanceStats", "MemoryMaintenance"]
