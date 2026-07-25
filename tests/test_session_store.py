from uuid import uuid4

import pytest

from boxkite.manager import SandboxManager
from boxkite.session_store import SQLiteSessionMetadataStore


@pytest.mark.asyncio
async def test_reconstruct_returns_none_when_nothing_saved(tmp_path):
    store = SQLiteSessionMetadataStore(tmp_path / "sessions.db")

    result = await store.reconstruct("never-recorded-session")

    assert result is None


@pytest.mark.asyncio
async def test_record_then_reconstruct_round_trips(tmp_path):
    store = SQLiteSessionMetadataStore(tmp_path / "sessions.db")
    org_id = uuid4()
    work_item_id = uuid4()

    await store.record(
        "session-1",
        organization_id=org_id,
        work_item_id=work_item_id,
        storage_prefix="work-items/org/wi",
        upload_file_ids=["file-a", "file-b"],
    )
    result = await store.reconstruct("session-1")

    assert result is not None
    assert result.organization_id == org_id
    assert result.work_item_id == work_item_id
    assert result.storage_prefix == "work-items/org/wi"
    assert result.upload_file_ids == ["file-a", "file-b"]


@pytest.mark.asyncio
async def test_reconstruct_survives_a_fresh_store_instance(tmp_path):
    """Simulates recovery after a process restart: a new store instance
    pointed at the same on-disk file must still find previously-recorded
    session metadata."""
    db_path = tmp_path / "sessions.db"
    writer = SQLiteSessionMetadataStore(db_path)
    await writer.record(
        "session-2",
        organization_id=None,
        work_item_id=None,
        storage_prefix="anon-prefix",
    )

    reader = SQLiteSessionMetadataStore(db_path)
    result = await reader.reconstruct("session-2")

    assert result is not None
    assert result.storage_prefix == "anon-prefix"
    assert result.organization_id is None
    assert result.upload_file_ids == []


@pytest.mark.asyncio
async def test_record_overwrites_existing_entry(tmp_path):
    store = SQLiteSessionMetadataStore(tmp_path / "sessions.db")
    await store.record(
        "session-3", organization_id=None, work_item_id=None, storage_prefix="v1"
    )
    await store.record(
        "session-3", organization_id=None, work_item_id=None, storage_prefix="v2"
    )

    result = await store.reconstruct("session-3")

    assert result.storage_prefix == "v2"


@pytest.mark.asyncio
async def test_forget_removes_recorded_session(tmp_path):
    store = SQLiteSessionMetadataStore(tmp_path / "sessions.db")
    await store.record(
        "session-4", organization_id=None, work_item_id=None, storage_prefix="p"
    )

    await store.forget("session-4")
    result = await store.reconstruct("session-4")

    assert result is None


@pytest.mark.asyncio
async def test_manager_record_session_metadata_calls_store_record(tmp_path):
    store = SQLiteSessionMetadataStore(tmp_path / "sessions.db")
    manager = SandboxManager(session_metadata_store=store)
    org_id = uuid4()

    await manager._record_session_metadata(
        "session-5",
        organization_id=org_id,
        work_item_id=None,
        storage_prefix="prefix-5",
        upload_file_ids=["f1"],
    )
    result = await store.reconstruct("session-5")

    assert result is not None
    assert result.organization_id == org_id
    assert result.storage_prefix == "prefix-5"


@pytest.mark.asyncio
async def test_manager_record_session_metadata_tolerates_store_without_record(tmp_path):
    """A store that only implements the SessionMetadataStore Protocol
    (reconstruct only, no record()) must not break session creation."""

    class ReconstructOnlyStore:
        async def reconstruct(self, session_id):
            return None

    manager = SandboxManager(session_metadata_store=ReconstructOnlyStore())

    await manager._record_session_metadata(
        "session-6",
        organization_id=None,
        work_item_id=None,
        storage_prefix="prefix-6",
    )


@pytest.mark.asyncio
async def test_manager_forget_session_metadata_calls_store_forget(tmp_path):
    store = SQLiteSessionMetadataStore(tmp_path / "sessions.db")
    manager = SandboxManager(session_metadata_store=store)
    await store.record(
        "session-7", organization_id=None, work_item_id=None, storage_prefix="p"
    )

    await manager._forget_session_metadata("session-7")
    result = await store.reconstruct("session-7")

    assert result is None
