"""Shared pytest fixtures for the control-plane test suite.

Tests exercise the FastAPI app in-process via httpx's ASGI transport (no
real network, no real Postgres, no real Kubernetes) — each test gets a
fresh SQLite file-backed database and a fake SandboxManager, so the fair-use
and cross-tenant logic under test is fully isolated from the real
`boxkite.SandboxManager` (which would otherwise try to reach the Kubernetes
API). The app's own lifespan (which starts the background reaper against the
*real* SandboxManager singleton) is deliberately NOT triggered here — schema
setup is done directly via `Base.metadata.create_all()`, and the reaper's
core logic (`reaper._reap_once`) is tested directly in test_usage_limits.py
instead.

Deliberately `create_all()`, NOT `db.init_schema()` (which runs the real
Alembic migrations, `../migrations/versions/`) -- every test starts from an
empty database, so there's no existing data an ALTER-only migration would
need to modify, and `create_all()` reads the exact same `Base.metadata`
those migrations are generated from. Running full Alembic machinery on
every one of this suite's ~650 tests measurably doubles the suite's
runtime for zero additional real-world coverage; `test_migrations.py`
covers the one thing this shortcut could actually miss -- migrations
silently drifting out of sync with `models_orm.py`.
"""

from __future__ import annotations

import fnmatch
import itertools
import re
import uuid
from collections.abc import AsyncGenerator

import httpx
import pytest

from control_plane import db as db_module
from control_plane.config import settings
from control_plane.deps import get_email_sender_dep, get_manager, get_snapshot_storage
from control_plane.enterprise_sso_client import EnterpriseSsoProfile
from control_plane.main import app
from control_plane.models_orm import Base
from control_plane.rate_limit import reset_rate_limits_for_tests
from control_plane.routers.demo_playground import reset_demo_create_lock_for_tests
from control_plane.usage_policy import reset_create_session_lock_for_tests


class FakeSandboxManager:
    """Stands in for boxkite.SandboxManager in tests — no K8s, no HTTP.

    `_files` is an in-memory dict per session_id so `/exec`, `/files`,
    `/files/view`, and `/files/str-replace` tests can assert real
    round-trip behavior (e.g. "content written by file_create shows up in
    view") without needing a real sidecar.
    """

    def __init__(self) -> None:
        self.created: dict[str, dict] = {}
        self.destroyed: list[str] = []
        self.fail_next_create = False
        self.exec_calls: list[dict] = []
        self.fail_next_exec = False
        self.http_request_calls: list[dict] = []
        self._files: dict[str, dict[str, str]] = {}
        self._processes: dict[str, dict] = {}
        self._process_id_counter = itertools.count(1)
        self.start_process_calls: list[dict] = []
        self.fail_next_start_process = False
        self.lsp_start_calls: list[dict] = []
        self.lsp_open_calls: list[dict] = []
        self.lsp_completion_calls: list[dict] = []
        self.lsp_stop_calls: list[dict] = []
        self.fail_next_lsp_start = False
        self.fail_next_lsp_completion = False
        self._lsp_id_counter = itertools.count(1)

    async def create_session(
        self,
        organization_id,
        session_id: str,
        work_item_id=None,
        upload_file_ids=None,
        size: str = "small",
        storage_gb=None,
        lifetime_seconds=None,
        restore_from_snapshot_id=None,
        secret_grants=None,
        secret_capability_token=None,
        secrets_control_plane_url=None,
        image_ref=None,
        volume_mounts=None,
        mcp_connection_grants=None,
        gpu_count=None,
    ) -> dict:
        if self.fail_next_create:
            self.fail_next_create = False
            raise RuntimeError("simulated SandboxManager failure")
        pod_name = f"fake-pod-{session_id[:8]}"
        self.created[session_id] = {
            "organization_id": organization_id,
            "pod_name": pod_name,
            "size": size,
            "storage_gb": storage_gb,
            "lifetime_seconds": lifetime_seconds,
            "restore_from_snapshot_id": restore_from_snapshot_id,
            "secret_grants": secret_grants,
            "secret_capability_token": secret_capability_token,
            "secrets_control_plane_url": secrets_control_plane_url,
            "image_ref": image_ref,
            "volume_mounts": volume_mounts,
            "mcp_connection_grants": mcp_connection_grants,
            "gpu_count": gpu_count,
        }
        self._files.setdefault(session_id, {})
        return {"pod_name": pod_name}

    async def snapshot(self, session_id: str) -> dict:
        """Fake confirmed-flush: returns whatever files test_snapshots.py
        seeded via self._files for this session, as storage keys relative
        to a fake storage_prefix -- mirrors SandboxManager.snapshot()'s real
        shape (`{"storage_prefix": ..., "storage_keys": [...]}`)."""
        files = self._files.get(session_id, {})
        storage_prefix = f"sessions/fake-org/{session_id}"
        return {
            "storage_prefix": storage_prefix,
            "storage_keys": [f"workspace/{path.lstrip('/')}" for path in files],
        }

    async def destroy_session(self, session_id: str, **_kwargs) -> None:
        self.destroyed.append(session_id)
        self.created.pop(session_id, None)

    async def execute(
        self,
        session_id: str,
        command: str,
        timeout: int = 30,
        description: str | None = None,
    ) -> dict:
        if self.fail_next_exec:
            self.fail_next_exec = False
            raise RuntimeError("simulated sidecar transport failure")
        self.exec_calls.append({"session_id": session_id, "command": command, "timeout": timeout})
        return {"exit_code": 0, "stdout": f"ran: {command}", "stderr": ""}

    async def http_request(
        self,
        session_id: str,
        method: str,
        url: str,
        headers: dict | None = None,
        body: str | None = None,
        timeout: int = 15,
    ) -> dict:
        self.http_request_calls.append(
            {"session_id": session_id, "method": method, "url": url, "headers": headers, "body": body, "timeout": timeout}
        )
        return {"status_code": 200, "headers": {"content-type": "text/plain"}, "body": "ok", "truncated": False}

    async def lsp_start(self, session_id: str, language: str) -> dict:
        if self.fail_next_lsp_start:
            self.fail_next_lsp_start = False
            raise RuntimeError("simulated sidecar transport failure")
        self.lsp_start_calls.append({"session_id": session_id, "language": language})
        return {"lsp_id": f"fake-lsp-{next(self._lsp_id_counter)}"}

    async def lsp_open(self, session_id: str, lsp_id: str, path: str, content: str) -> dict:
        self.lsp_open_calls.append(
            {"session_id": session_id, "lsp_id": lsp_id, "path": path, "content": content}
        )
        return {"status": "ok"}

    async def lsp_completion(
        self, session_id: str, lsp_id: str, path: str, line: int, character: int
    ) -> dict:
        if self.fail_next_lsp_completion:
            self.fail_next_lsp_completion = False
            raise RuntimeError("simulated sidecar transport failure")
        self.lsp_completion_calls.append(
            {"session_id": session_id, "lsp_id": lsp_id, "path": path, "line": line, "character": character}
        )
        return {"items": []}

    async def lsp_stop(self, session_id: str, lsp_id: str) -> dict:
        self.lsp_stop_calls.append({"session_id": session_id, "lsp_id": lsp_id})
        return {"status": "ok"}

    async def file_create(
        self,
        session_id: str,
        path: str,
        content: str,
        description: str | None = None,
    ) -> dict:
        files = self._files.setdefault(session_id, {})
        created = path not in files
        files[path] = content
        return {"path": path, "size": len(content.encode("utf-8")), "created": created}

    async def view(
        self,
        session_id: str,
        path: str,
        view_range: list[int] | None = None,
        description: str | None = None,
    ) -> dict:
        files = self._files.get(session_id, {})
        if path not in files:
            raise FileNotFoundError(f"File not found: {path}")
        content = files[path]
        return {"content": content, "lines": content.count("\n") + 1, "is_directory": False, "entries": None}

    async def str_replace(
        self,
        session_id: str,
        path: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
        description: str | None = None,
    ) -> dict:
        files = self._files.get(session_id, {})
        if path not in files:
            raise FileNotFoundError(f"File not found: {path}")
        content = files[path]
        occurrences = content.count(old_str)
        if occurrences == 0 or (occurrences > 1 and not replace_all):
            return {"path": path, "replaced": False, "occurrences": occurrences}
        files[path] = content.replace(old_str, new_str)
        return {"path": path, "replaced": True, "occurrences": occurrences}

    async def ls(self, session_id: str, path: str = "/") -> list[dict]:
        files = self._files.get(session_id, {})
        return [{"path": p, "is_dir": False, "size": len(c.encode("utf-8"))} for p, c in files.items()]

    async def glob(self, session_id: str, pattern: str, path: str = "/") -> list[dict]:
        files = self._files.get(session_id, {})
        return [
            {"path": p, "is_dir": False, "size": len(c.encode("utf-8"))}
            for p, c in files.items()
            if fnmatch.fnmatch(p, pattern)
        ]

    async def grep(
        self,
        session_id: str,
        pattern: str,
        path: str | None = "/",
        glob: str | None = None,
        max_matches: int = 500,
    ) -> dict:
        files = self._files.get(session_id, {})
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            return {"matches": [], "error": str(exc), "truncated": False}
        matches: list[dict] = []
        truncated = False
        for file_path, content in files.items():
            if glob is not None and not fnmatch.fnmatch(file_path, glob):
                continue
            for line_no, line in enumerate(content.splitlines(), start=1):
                if regex.search(line):
                    if len(matches) >= max_matches:
                        truncated = True
                        break
                    matches.append({"path": file_path, "line": line_no, "text": line})
            if truncated:
                break
        return {"matches": matches, "error": None, "truncated": truncated}

    async def start_process(
        self,
        session_id: str,
        command: str,
        description: str | None = None,
        max_runtime_seconds: int = 3600,
        expose_port: int | None = None,
    ) -> dict:
        if self.fail_next_start_process:
            self.fail_next_start_process = False
            raise RuntimeError("simulated sidecar transport failure")
        self.start_process_calls.append(
            {
                "session_id": session_id,
                "command": command,
                "description": description,
                "max_runtime_seconds": max_runtime_seconds,
                "expose_port": expose_port,
            }
        )
        process_id = f"proc_fake{next(self._process_id_counter)}"
        entry = {
            "process_id": process_id,
            "command": command,
            "description": description,
            "status": "running",
            "started_at": "2026-07-11T00:00:00",
            "exit_code": None,
            "stdout": "",
            "expose_port": expose_port,
        }
        self._processes.setdefault(session_id, {})[process_id] = entry
        return {
            "process_id": process_id,
            "status": entry["status"],
            "started_at": entry["started_at"],
        }

    async def get_process_output(
        self,
        session_id: str,
        process_id: str,
        since_offset: int = 0,
    ) -> dict:
        entry = self._processes.get(session_id, {}).get(process_id)
        if entry is None:
            raise ValueError(f"Process not found: {process_id}")
        stdout = entry["stdout"]
        chunk = stdout[since_offset:]
        return {
            "status": entry["status"],
            "stdout_chunk": chunk,
            "next_offset": len(stdout),
            "truncated": False,
            "exit_code": entry["exit_code"],
        }

    async def send_process_input(self, session_id: str, process_id: str, data: str) -> dict:
        entry = self._processes.get(session_id, {}).get(process_id)
        if entry is None:
            raise ValueError(f"Process not found: {process_id}")
        return {"bytes_written": len(data.encode("utf-8"))}

    async def stop_process(self, session_id: str, process_id: str) -> dict:
        entry = self._processes.get(session_id, {}).get(process_id)
        if entry is None:
            raise ValueError(f"Process not found: {process_id}")
        entry["status"] = "stopped"
        entry["exit_code"] = 143
        return {"status": entry["status"], "exit_code": entry["exit_code"]}

    async def list_processes(self, session_id: str) -> list[dict]:
        entries = self._processes.get(session_id, {}).values()
        return [
            {
                "process_id": e["process_id"],
                "command": e["command"],
                "description": e["description"],
                "status": e["status"],
                "started_at": e["started_at"],
                "exit_code": e["exit_code"],
                "expose_port": e.get("expose_port"),
            }
            for e in entries
        ]

    async def get_sidecar_pty_target(self, session_id: str) -> dict:
        """Fake resolution for `WS .../takeover` (routers/sandboxes.py's
        `takeover_sandbox`) -- a fixed, fake sidecar WS URL/token. Tests that
        need to drive the full takeover route (not just
        `_authenticate_takeover_or_close`) also monkeypatch
        `control_plane.routers.sandboxes.websockets.connect` so this URL is
        never dialed for real; this method only needs to return a
        plausible-shaped dict, not a reachable one."""
        return {
            "ws_url": f"wss://fake-sidecar.example/{session_id}/pty",
            "auth_header": "X-Sidecar-Auth-Token",
            "auth_token": "fake-sidecar-token",
        }

    async def proxy_preview_request(
        self,
        session_id: str,
        port: int,
        path: str,
        method: str,
        params: dict | None = None,
        headers: dict | None = None,
        content: bytes = b"",
    ):
        """Fake preview proxy: returns a canned httpx.Response so tests can
        exercise the control-plane preview route without a real sidecar."""
        import httpx

        return httpx.Response(
            status_code=200,
            headers={"content-type": "text/plain"},
            content=f"preview:{session_id}:{port}:{path}".encode("utf-8"),
        )


class FakeSnapshotStorageClient:
    """Stands in for storage_client.SnapshotStorageClient in tests -- an
    in-memory dict of prefix -> {relative_key: fake_byte_size}, so
    copy_prefix/list_keys/delete_prefix round-trip without any real S3/Azure
    credential. `fail_next_copy`/`fail_next_delete` let tests assert the
    control plane's own failure-handling paths (mark_failed, 502s) without
    a real storage outage."""

    def __init__(self) -> None:
        self._objects: dict[str, dict[str, int]] = {}
        self.fail_next_copy = False
        self.fail_next_delete = False
        self.copy_calls: list[dict] = []
        self.delete_calls: list[str] = []
        # Separate from the prefix-copy `_objects` dict above: upload_bytes/
        # download_bytes (added for takeover recordings, GitHub issue #133)
        # address single, exact object keys rather than a prefix.
        self._blobs: dict[str, bytes] = {}
        self.fail_next_upload = False
        self.upload_calls: list[dict] = []

    def seed(self, prefix: str, keys: dict[str, int]) -> None:
        """Test helper: populate `prefix` with `{relative_key: size_bytes}`
        as if a prior copy/upload had already happened."""
        self._objects.setdefault(prefix, {}).update(keys)

    async def copy_prefix(self, *, source_prefix: str, dest_prefix: str, keys: list[str]) -> int:
        if self.fail_next_copy:
            self.fail_next_copy = False
            raise RuntimeError("simulated snapshot storage copy failure")
        self.copy_calls.append({"source_prefix": source_prefix, "dest_prefix": dest_prefix, "keys": list(keys)})
        source = self._objects.get(source_prefix, {})
        dest = self._objects.setdefault(dest_prefix, {})
        total = 0
        for key in keys:
            size = source.get(key, 0)
            dest[key] = size
            total += size
        return total

    async def delete_prefix(self, *, prefix: str) -> None:
        if self.fail_next_delete:
            self.fail_next_delete = False
            raise RuntimeError("simulated snapshot storage delete failure")
        self.delete_calls.append(prefix)
        self._objects.pop(prefix, None)

    async def list_keys(self, *, prefix: str) -> list[str]:
        return list(self._objects.get(prefix, {}).keys())

    async def upload_bytes(self, *, key: str, data: bytes, content_type: str = "application/octet-stream") -> None:
        if self.fail_next_upload:
            self.fail_next_upload = False
            raise RuntimeError("simulated snapshot storage upload failure")
        self.upload_calls.append({"key": key, "content_type": content_type, "size": len(data)})
        self._blobs[key] = data

    async def download_bytes(self, *, key: str) -> bytes:
        return self._blobs[key]


@pytest.fixture
async def fake_snapshot_storage() -> FakeSnapshotStorageClient:
    return FakeSnapshotStorageClient()


class FakeEmailSender:
    """Stands in for email_sender.EmailSender in tests -- records every
    call so tests can assert a reset/verification email was "sent" (and to
    which address, with which raw token) without a real mail transport."""

    def __init__(self) -> None:
        self.password_reset_calls: list[dict] = []
        self.verification_calls: list[dict] = []

    async def send_password_reset_email(self, *, to_email: str, reset_token: str) -> None:
        self.password_reset_calls.append({"to_email": to_email, "reset_token": reset_token})

    async def send_verification_email(self, *, to_email: str, verification_token: str) -> None:
        self.verification_calls.append({"to_email": to_email, "verification_token": verification_token})


@pytest.fixture
async def fake_email_sender() -> FakeEmailSender:
    return FakeEmailSender()


class FakeEnterpriseSsoClient:
    """Stands in for enterprise_sso_client.EnterpriseSsoClient in tests --
    an in-memory mapping of authorization `code` -> profile, so
    routers/enterprise_sso.py's account-resolution logic can be tested
    without a real WorkOS account/IdP. `authorization_url` records every
    call so tests can assert connection_selector/redirect_uri/state were
    threaded through correctly, same "record calls, no real network"
    pattern FakeSnapshotStorageClient/FakeEmailSender above use."""

    def __init__(self) -> None:
        self.authorize_calls: list[dict] = []
        self._profiles_by_code: dict[str, EnterpriseSsoProfile] = {}

    def seed_profile(self, code: str, profile: EnterpriseSsoProfile) -> None:
        self._profiles_by_code[code] = profile

    def authorization_url(self, *, connection_selector: str, redirect_uri: str, state: str) -> str:
        self.authorize_calls.append(
            {"connection_selector": connection_selector, "redirect_uri": redirect_uri, "state": state}
        )
        return f"https://fake-sso-broker.example.com/authorize?connection={connection_selector}&state={state}"

    async def fetch_profile(self, *, code: str, redirect_uri: str) -> EnterpriseSsoProfile:
        profile = self._profiles_by_code.get(code)
        if profile is None:
            from control_plane.errors import ApiError

            raise ApiError(401, "enterprise_sso_failed", "SSO provider rejected the authorization code")
        return profile


@pytest.fixture
async def fake_enterprise_sso_client() -> FakeEnterpriseSsoClient:
    return FakeEnterpriseSsoClient()


@pytest.fixture(autouse=True)
def _reset_rate_limits():
    reset_rate_limits_for_tests()
    yield
    reset_rate_limits_for_tests()


@pytest.fixture(autouse=True)
def _reset_takeover_jti_guard():
    from control_plane.routers.sandboxes import reset_takeover_jti_guard_for_tests

    reset_takeover_jti_guard_for_tests()
    yield
    reset_takeover_jti_guard_for_tests()


@pytest.fixture(autouse=True)
def _reset_sandbox_create_jti_guard():
    from control_plane.deps import reset_sandbox_create_jti_guard_for_tests

    reset_sandbox_create_jti_guard_for_tests()
    yield
    reset_sandbox_create_jti_guard_for_tests()


@pytest.fixture(autouse=True)
def _reset_takeover_recordings_registry():
    from control_plane.routers.sandboxes import reset_takeover_recordings_registry_for_tests

    reset_takeover_recordings_registry_for_tests()
    yield
    reset_takeover_recordings_registry_for_tests()


@pytest.fixture(autouse=True)
def _reset_create_session_lock():
    # Rebinds the asyncio.Lock to this test's own event loop -- see
    # reset_create_session_lock_for_tests()'s docstring for why this is
    # needed only in tests, never in production.
    reset_create_session_lock_for_tests()
    reset_demo_create_lock_for_tests()
    yield


@pytest.fixture(autouse=True)
def _reset_exec_log_chain_locks():
    from control_plane.audit_chain_lock import reset_exec_log_chain_locks_for_tests

    # Same per-test event-loop rebind reasoning as _reset_create_session_lock
    # above -- see reset_exec_log_chain_locks_for_tests()'s own docstring.
    reset_exec_log_chain_locks_for_tests()
    yield


@pytest.fixture
async def fake_manager() -> FakeSandboxManager:
    return FakeSandboxManager()


@pytest.fixture
async def client(
    tmp_path,
    fake_manager: FakeSandboxManager,
    fake_snapshot_storage: FakeSnapshotStorageClient,
    fake_email_sender: FakeEmailSender,
) -> AsyncGenerator[httpx.AsyncClient, None]:
    db_path = tmp_path / f"test_{uuid.uuid4().hex}.db"
    settings.DATABASE_URL = f"sqlite+aiosqlite:///{db_path}"
    # Force a fresh engine/session-factory bound to this test's own database
    # file — module-level singletons in db.py must not leak across tests.
    db_module._engine = None
    db_module._session_factory = None
    engine = db_module.get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    app.dependency_overrides[get_manager] = lambda: fake_manager
    app.dependency_overrides[get_snapshot_storage] = lambda: fake_snapshot_storage
    app.dependency_overrides[get_email_sender_dep] = lambda: fake_email_sender

    transport = httpx.ASGITransport(app=app, client=("testclient", 12345))
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as ac:
        yield ac

    app.dependency_overrides.clear()
    await db_module.dispose_engine()


async def signup(client: httpx.AsyncClient, email: str, password: str = "hunter2pass") -> dict:
    resp = await client.post("/v1/auth/signup", json={"email": email, "password": password})
    assert resp.status_code == 201, resp.text
    return resp.json()


async def create_api_key(
    client: httpx.AsyncClient, access_token: str, name: str = "test key", role: str = "admin"
) -> dict:
    resp = await client.post(
        "/v1/api-keys",
        json={"name": name, "role": role},
        headers={"Authorization": f"Bearer {access_token}"},
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def signup_and_get_api_key(client: httpx.AsyncClient, email: str, role: str = "admin") -> str:
    token_response = await signup(client, email)
    key_response = await create_api_key(client, token_response["access_token"], role=role)
    return key_response["key"]
