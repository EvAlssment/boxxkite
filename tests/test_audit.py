from uuid import uuid4

import pytest

from boxkite.audit import NoOpAuditSink, SQLiteAuditSink, safe_call


@pytest.mark.asyncio
async def test_record_file_write_then_list_for_session(tmp_path):
    sink = SQLiteAuditSink(tmp_path / "audit.db")
    org_id = uuid4()

    await sink.record_file_write(
        organization_id=org_id,
        work_item_id=None,
        session_id="session-1",
        agent_name="agent-a",
        file_path="/tmp/out.txt",
        content=b"hello world",
    )
    entries = await sink.list_for_session("session-1")

    assert len(entries) == 1
    entry = entries[0]
    assert entry.kind == "file_write"
    assert entry.organization_id == str(org_id)
    assert entry.session_id == "session-1"
    assert entry.agent_name == "agent-a"
    assert entry.detail == {"file_path": "/tmp/out.txt", "size_bytes": 11}


@pytest.mark.asyncio
async def test_record_file_registered(tmp_path):
    sink = SQLiteAuditSink(tmp_path / "audit.db")

    await sink.record_file_registered(
        organization_id=None,
        work_item_id=None,
        session_id="session-2",
        agent_name=None,
        file_path="/tmp/report.pdf",
        storage_key="org/session-2/report.pdf",
        size_bytes=4096,
    )
    entries = await sink.list_for_session("session-2")

    assert entries[0].kind == "file_registered"
    assert entries[0].detail == {
        "file_path": "/tmp/report.pdf",
        "storage_key": "org/session-2/report.pdf",
        "size_bytes": 4096,
    }


@pytest.mark.asyncio
async def test_record_exec(tmp_path):
    sink = SQLiteAuditSink(tmp_path / "audit.db")

    await sink.record_exec(
        organization_id=None,
        work_item_id=None,
        session_id="session-3",
        agent_name="agent-b",
        command="ls -la",
        exit_code=0,
        duration_ms=42,
    )
    entries = await sink.list_for_session("session-3")

    assert entries[0].kind == "exec"
    assert entries[0].detail == {
        "command": "ls -la",
        "exit_code": 0,
        "duration_ms": 42,
    }


@pytest.mark.asyncio
async def test_list_for_session_replays_in_recorded_order(tmp_path):
    sink = SQLiteAuditSink(tmp_path / "audit.db")

    await sink.record_exec(
        organization_id=None, work_item_id=None, session_id="session-4",
        agent_name=None, command="one", exit_code=0, duration_ms=1,
    )
    await sink.record_exec(
        organization_id=None, work_item_id=None, session_id="session-4",
        agent_name=None, command="two", exit_code=0, duration_ms=1,
    )
    await sink.record_exec(
        organization_id=None, work_item_id=None, session_id="session-4",
        agent_name=None, command="three", exit_code=0, duration_ms=1,
    )

    entries = await sink.list_for_session("session-4")

    assert [e.detail["command"] for e in entries] == ["one", "two", "three"]


@pytest.mark.asyncio
async def test_list_for_session_only_returns_matching_session(tmp_path):
    sink = SQLiteAuditSink(tmp_path / "audit.db")
    await sink.record_exec(
        organization_id=None, work_item_id=None, session_id="session-a",
        agent_name=None, command="a", exit_code=0, duration_ms=1,
    )
    await sink.record_exec(
        organization_id=None, work_item_id=None, session_id="session-b",
        agent_name=None, command="b", exit_code=0, duration_ms=1,
    )

    entries = await sink.list_for_session("session-a")

    assert len(entries) == 1
    assert entries[0].detail["command"] == "a"


@pytest.mark.asyncio
async def test_list_for_session_empty_when_nothing_recorded(tmp_path):
    sink = SQLiteAuditSink(tmp_path / "audit.db")

    entries = await sink.list_for_session("never-recorded")

    assert entries == []


@pytest.mark.asyncio
async def test_get_download_url_always_none(tmp_path):
    sink = SQLiteAuditSink(tmp_path / "audit.db")

    result = await sink.get_download_url(
        organization_id=None, work_item_id=None,
        file_path="/tmp/x", storage_key="k", expiry_seconds=60,
    )

    assert result is None


@pytest.mark.asyncio
async def test_entries_survive_a_fresh_sink_instance(tmp_path):
    db_path = tmp_path / "audit.db"
    writer = SQLiteAuditSink(db_path)
    await writer.record_exec(
        organization_id=None, work_item_id=None, session_id="session-5",
        agent_name=None, command="persisted", exit_code=0, duration_ms=1,
    )

    reader = SQLiteAuditSink(db_path)
    entries = await reader.list_for_session("session-5")

    assert len(entries) == 1
    assert entries[0].detail["command"] == "persisted"


@pytest.mark.asyncio
async def test_safe_call_uses_sqlite_audit_sink(tmp_path):
    sink = SQLiteAuditSink(tmp_path / "audit.db")

    await safe_call(
        sink,
        "record_exec",
        organization_id=None, work_item_id=None, session_id="session-6",
        agent_name=None, command="via-safe-call", exit_code=0, duration_ms=1,
    )
    entries = await sink.list_for_session("session-6")

    assert entries[0].detail["command"] == "via-safe-call"


@pytest.mark.asyncio
async def test_noop_audit_sink_unaffected():
    sink = NoOpAuditSink()

    assert await sink.record_file_write() is None
    assert await sink.record_exec() is None
    assert await sink.get_download_url() is None
