"""Bounded, account-scoped MemoryBase maintenance tests."""

from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from conftest import signup_and_get_api_key
from control_plane import db as db_module
from control_plane.memory_embeddings import MemoryVectorStore
from control_plane.memory_maintenance import MemoryMaintenance
from control_plane.models_orm import Account, MemoryEmbedding, MemoryRecord, MemoryRelation


def _record(*, account_id: str, content: str, scope: str = "default", kind: str = "fact") -> MemoryRecord:
    now = datetime.now(timezone.utc)
    return MemoryRecord(
        account_id=account_id,
        scope=scope,
        kind=kind,
        content=content,
        content_hash=hashlib.sha256(content.encode()).hexdigest(),
        created_at=now,
        updated_at=now,
    )


async def _account_id(email: str) -> str:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(select(Account).where(Account.email == email))
        return result.scalar_one().id


async def test_purge_expired_is_bounded_idempotent_and_account_scoped(client):
    await signup_and_get_api_key(client, "maintenance-purge-a@example.com")
    await signup_and_get_api_key(client, "maintenance-purge-b@example.com")
    account_a = await _account_id("maintenance-purge-a@example.com")
    account_b = await _account_id("maintenance-purge-b@example.com")
    now = datetime.now(timezone.utc)

    async with db_module.get_session_factory()() as db:
        expired_a = _record(account_id=account_a, content="expired A")
        expired_a.expires_at = now - timedelta(minutes=1)
        expired_b = _record(account_id=account_b, content="expired B")
        expired_b.expires_at = now - timedelta(minutes=1)
        db.add_all([expired_a, expired_b])
        await db.commit()

    async with db_module.get_session_factory()() as db:
        stats = await MemoryMaintenance(db).purge_expired(
            account_id=account_a, batch_size=1, max_batches=1, now=now
        )
        assert stats.deleted == 1
        assert stats.has_more is True

    async with db_module.get_session_factory()() as db:
        remaining_b = await db.execute(
            select(MemoryRecord).where(
                MemoryRecord.account_id == account_b,
                MemoryRecord.content == "expired B",
            )
        )
        assert remaining_b.scalar_one_or_none() is not None
        second = await MemoryMaintenance(db).purge_expired(account_id=account_a, now=now)
        assert second.deleted == 0


async def test_compact_dry_run_apply_rewire_and_idempotency(client):
    await signup_and_get_api_key(client, "maintenance-compact@example.com")
    account_id = await _account_id("maintenance-compact@example.com")
    async with db_module.get_session_factory()() as db:
        exact_survivor = _record(account_id=account_id, content="same durable fact")
        exact_duplicate = _record(account_id=account_id, content="same durable fact", kind="note")
        near = _record(
            account_id=account_id,
            content="The service uses a blue green deployment rollout",
        )
        near_duplicate = _record(
            account_id=account_id,
            content="The service uses a blue green deployment rollout strategy",
        )
        target = _record(account_id=account_id, content="deployment target")
        db.add_all([exact_survivor, exact_duplicate, near, near_duplicate, target])
        await db.flush()
        db.add(
            MemoryRelation(
                account_id=account_id,
                source_memory_id=exact_duplicate.id,
                target_memory_id=target.id,
                relation_type="related",
                confidence=0.8,
            )
        )
        await db.commit()

    async with db_module.get_session_factory()() as db:
        maintenance = MemoryMaintenance(db)
        dry = await maintenance.compact(
            account_id=account_id,
            batch_size=10,
            near_duplicate_threshold=0.75,
            dry_run=True,
        )
        assert dry.dry_run is True
        assert dry.merged >= 2
        count_before = await db.execute(
            select(MemoryRecord).where(MemoryRecord.account_id == account_id)
        )
        assert len(count_before.scalars().all()) == 5

        applied = await maintenance.compact(
            account_id=account_id,
            batch_size=10,
            near_duplicate_threshold=0.75,
        )
        assert applied.merged >= 2
        assert applied.superseded >= 2
        assert applied.relations_rewired == 1
        again = await maintenance.compact(
            account_id=account_id,
            batch_size=10,
            near_duplicate_threshold=0.75,
        )
        assert again.merged == 0
        assert again.deleted == 0
        assert again.superseded == 0

        records = await db.execute(select(MemoryRecord).where(MemoryRecord.account_id == account_id))
        assert len(records.scalars().all()) == 5
        edges = await db.execute(
            select(MemoryRelation).where(MemoryRelation.account_id == account_id)
        )
        edge = edges.scalar_one()
        assert edge.source_memory_id == exact_survivor.id
        assert edge.target_memory_id == target.id


async def test_rebuild_relations_is_reconciliatory_and_idempotent(client):
    await signup_and_get_api_key(client, "maintenance-relations@example.com")
    account_id = await _account_id("maintenance-relations@example.com")
    async with db_module.get_session_factory()() as db:
        source = _record(account_id=account_id, content="Python is used for data analysis")
        target = _record(account_id=account_id, content="Python is used for data analysis notebooks")
        stale = _record(account_id=account_id, content="unrelated memory")
        db.add_all([source, target, stale])
        await db.flush()
        db.add(
            MemoryRelation(
                account_id=account_id,
                source_memory_id=source.id,
                target_memory_id=stale.id,
                relation_type="related",
                confidence=0.1,
            )
        )
        await db.commit()

        first = await MemoryMaintenance(db).rebuild_relations(
            account_id=account_id, batch_size=10
        )
        assert first.relations_added == 2
        assert first.relations_removed == 1
        second = await MemoryMaintenance(db).rebuild_relations(
            account_id=account_id, batch_size=10
        )
        assert second.relations_added == 0
        assert second.relations_removed == 0
        edges = await db.execute(
            select(MemoryRelation).where(
                MemoryRelation.account_id == account_id,
                MemoryRelation.source_memory_id == source.id,
            )
        )
        result = edges.scalars().all()
        assert {edge.target_memory_id for edge in result} == {target.id}


async def test_embedding_backfill_is_bounded_idempotent_and_account_scoped(client):
    class Provider:
        async def embed(self, texts):
            return [[1.0, 0.0] for _text in texts]

    await signup_and_get_api_key(client, "maintenance-embed-a@example.com")
    await signup_and_get_api_key(client, "maintenance-embed-b@example.com")
    account_a = await _account_id("maintenance-embed-a@example.com")
    account_b = await _account_id("maintenance-embed-b@example.com")
    async with db_module.get_session_factory()() as db:
        db.add_all(
            [
                _record(account_id=account_a, content="first account A memory"),
                _record(account_id=account_a, content="second account A memory"),
                _record(account_id=account_b, content="account B memory"),
            ]
        )
        await db.commit()
        store = MemoryVectorStore(db, Provider(), model_id="test-2d-v1", dimensions=2)
        dry = await MemoryMaintenance(db).backfill_embeddings(
            account_id=account_a,
            vector_store=store,
            batch_size=1,
            max_batches=1,
            dry_run=True,
        )
        assert dry.embedded == 1
        assert dry.has_more is True
        count = await db.execute(select(MemoryEmbedding))
        assert count.scalars().all() == []

        applied = await MemoryMaintenance(db).backfill_embeddings(
            account_id=account_a,
            vector_store=store,
            batch_size=10,
        )
        assert applied.embedded == 2
        again = await MemoryMaintenance(db).backfill_embeddings(
            account_id=account_a,
            vector_store=store,
            batch_size=10,
        )
        assert again.embedded == 0
        embedded = await db.execute(select(MemoryEmbedding))
        rows = embedded.scalars().all()
        assert len(rows) == 2
        assert {row.account_id for row in rows} == {account_a}


def test_maintenance_bounds_are_rejected():
    with pytest.raises(ValueError):
        from control_plane.memory_maintenance import _validate_bounds

        _validate_bounds(0, 1)
    with pytest.raises(ValueError):
        from control_plane.memory_maintenance import _validate_bounds

        _validate_bounds(1, 101)
