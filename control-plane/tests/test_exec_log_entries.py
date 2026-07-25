"""ExecLogEntry audit-log coverage: every one of the seven sandbox
exec/file-op routes must write exactly one row to `exec_log_entries` on
success, with `source="agent"`, the right `operation` name, and
operation-specific `detail`. See `docs/SANDBOX-OBSERVABILITY-DESIGN.md`
section 3.

Follows test_sandbox_exec.py's fixtures (`client`, `fake_manager`,
`signup_and_get_api_key`) and test_usage_limits.py's pattern for querying
rows directly via `db_module.get_session_factory()`.
"""

from __future__ import annotations

import httpx
from sqlalchemy import select

from control_plane import db as db_module
from control_plane.models_orm import ExecLogEntry

from conftest import FakeSandboxManager, signup_and_get_api_key


async def _create_session(client: httpx.AsyncClient, api_key: str) -> str:
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {api_key}"})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


async def _log_entries_for_session(session_id: str) -> list[ExecLogEntry]:
    async with db_module.get_session_factory()() as db:
        result = await db.execute(
            select(ExecLogEntry).where(ExecLogEntry.session_id == session_id).order_by(ExecLogEntry.started_at)
        )
        return list(result.scalars().all())


async def test_exec_writes_an_exec_log_entry(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "log-exec@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hello", "timeout": 45},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200

    entries = await _log_entries_for_session(session_id)
    assert len(entries) == 1
    entry = entries[0]
    assert entry.source == "agent"
    assert entry.operation == "exec"
    assert entry.detail == {"command": "echo hello", "timeout": 45}
    assert entry.exit_code == 0
    assert entry.output_truncated == "ran: echo hello"
    assert entry.duration_ms >= 0
    assert entry.account_id
    assert entry.session_id == session_id


async def test_failed_exec_does_not_write_a_log_entry(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "log-exec-fail@example.com")
    session_id = await _create_session(client, key)
    fake_manager.fail_next_exec = True

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 502

    assert await _log_entries_for_session(session_id) == []


async def test_file_create_writes_an_exec_log_entry(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "log-file-create@example.com")
    session_id = await _create_session(client, key)

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "hello.txt", "content": "hi"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200

    entries = await _log_entries_for_session(session_id)
    assert len(entries) == 1
    assert entries[0].operation == "file_create"
    assert entries[0].detail == {"path": "hello.txt"}
    assert entries[0].source == "agent"


async def test_file_view_writes_an_exec_log_entry(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "log-file-view@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "hello.txt", "content": "hi"},
        headers={"Authorization": f"Bearer {key}"},
    )

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/view",
        json={"path": "hello.txt"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200

    entries = await _log_entries_for_session(session_id)
    view_entries = [e for e in entries if e.operation == "view"]
    assert len(view_entries) == 1
    assert view_entries[0].detail["path"] == "hello.txt"


async def test_str_replace_writes_an_exec_log_entry(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "log-str-replace@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "config.py", "content": "DEBUG = False"},
        headers={"Authorization": f"Bearer {key}"},
    )

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/str-replace",
        json={"path": "config.py", "old_str": "False", "new_str": "True"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200

    entries = await _log_entries_for_session(session_id)
    replace_entries = [e for e in entries if e.operation == "str_replace"]
    assert len(replace_entries) == 1
    assert replace_entries[0].detail == {"path": "config.py", "replace_all": False}


async def test_ls_writes_an_exec_log_entry(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "log-ls@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "hello.txt", "content": "hi"},
        headers={"Authorization": f"Bearer {key}"},
    )

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/ls",
        json={"path": "/"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200

    entries = await _log_entries_for_session(session_id)
    ls_entries = [e for e in entries if e.operation == "ls"]
    assert len(ls_entries) == 1
    assert ls_entries[0].detail["path"] == "/"


async def test_glob_writes_an_exec_log_entry(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "log-glob@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "hello.txt", "content": "hi"},
        headers={"Authorization": f"Bearer {key}"},
    )

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/glob",
        json={"pattern": "*.txt", "path": "/"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200

    entries = await _log_entries_for_session(session_id)
    glob_entries = [e for e in entries if e.operation == "glob"]
    assert len(glob_entries) == 1
    assert glob_entries[0].detail["pattern"] == "*.txt"
    assert glob_entries[0].detail["match_count"] == 1


async def test_grep_writes_an_exec_log_entry(client: httpx.AsyncClient, fake_manager: FakeSandboxManager):
    key = await signup_and_get_api_key(client, "log-grep@example.com")
    session_id = await _create_session(client, key)
    await client.post(
        f"/v1/sandboxes/{session_id}/files",
        json={"path": "hello.txt", "content": "hi there"},
        headers={"Authorization": f"Bearer {key}"},
    )

    resp = await client.post(
        f"/v1/sandboxes/{session_id}/files/grep",
        json={"pattern": "hi", "path": "/"},
        headers={"Authorization": f"Bearer {key}"},
    )
    assert resp.status_code == 200

    entries = await _log_entries_for_session(session_id)
    grep_entries = [e for e in entries if e.operation == "grep"]
    assert len(grep_entries) == 1
    assert grep_entries[0].detail["pattern"] == "hi"
    assert grep_entries[0].detail["match_count"] == 1


async def test_exec_log_entries_are_scoped_to_the_owning_account(
    client: httpx.AsyncClient, fake_manager: FakeSandboxManager
):
    key = await signup_and_get_api_key(client, "log-account-scope@example.com")
    session_id = await _create_session(client, key)

    await client.post(
        f"/v1/sandboxes/{session_id}/exec",
        json={"command": "echo hi"},
        headers={"Authorization": f"Bearer {key}"},
    )

    async with db_module.get_session_factory()() as db:
        resp = await db.execute(select(ExecLogEntry).where(ExecLogEntry.session_id == session_id))
        entry = resp.scalar_one()

    from control_plane.repository import AccountRepository

    async with db_module.get_session_factory()() as db:
        account = await AccountRepository(db).get_by_email("log-account-scope@example.com")
    assert entry.account_id == account.id
