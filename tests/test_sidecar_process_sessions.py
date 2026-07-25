"""Tests for the background process registry (/process/*) — see
docs/PROCESS-SESSIONS-DESIGN.md.

These exercise real subprocesses in RUNTIME_MODE="compose" (bypassing
nsenter/docker-exec, same pattern test_sidecar_exec_output_cap.py already
uses for exec_in_sandbox) so the reader loop, ring buffer, watchdog, and
kill-all paths run against actual asyncio.subprocess.Process objects rather
than mocks.
"""

import asyncio

import main as sidecar_main
from fastapi.testclient import TestClient

AUTH_TOKEN = "the-real-secret"


def _client() -> TestClient:
    """A persistent-portal TestClient: background tasks created by
    /process/start (reader loop, watchdog) run in the sidecar app's event
    loop and must outlive a single request. Using the `with` form pins every
    request from this client to the same underlying event loop instead of
    spinning up a fresh one per call, which is required for asyncio tasks
    to survive across requests."""
    return TestClient(sidecar_main.app).__enter__()


def _headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: AUTH_TOKEN}


async def _fake_spawn(command: str, *, expose_network: bool = False):
    """Bypass nsenter/docker-exec entirely and just run `command` as a real
    shell subprocess with a stdin pipe and merged stdout/stderr -- mirrors
    what _spawn_background_process would hand back, minus the sandbox
    namespace plumbing this test doesn't need."""
    return await asyncio.create_subprocess_shell(
        command,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )


def _reset_registry():
    sidecar_main._process_registry.clear()
    sidecar_main._exposed_ports.clear()
    sidecar_main._process_registry_lock = None


def setup_function(_):
    _reset_registry()


def teardown_function(_):
    _reset_registry()


def test_process_start_returns_running_status(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _fake_spawn)
    client = _client()

    response = client.post(
        "/process/start",
        json={"command": "sleep 0.2", "max_runtime_seconds": 30},
        headers=_headers(),
    )
    assert response.status_code == 201
    body = response.json()
    assert body["process_id"].startswith("proc_")
    assert body["status"] == "running"
    assert "started_at" in body


def test_process_start_rejects_max_runtime_seconds_over_ceiling(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "PROCESS_MAX_RUNTIME_SECONDS_CEILING", 60)
    client = _client()

    response = client.post(
        "/process/start",
        json={"command": "sleep 0.1", "max_runtime_seconds": 61},
        headers=_headers(),
    )
    assert response.status_code == 400
    assert "max_runtime_seconds" in response.json()["detail"]


def test_process_start_enforces_concurrency_cap(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "SANDBOX_MAX_BACKGROUND_PROCESSES", 1)
    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _fake_spawn)
    client = _client()

    first = client.post(
        "/process/start",
        json={"command": "sleep 5", "max_runtime_seconds": 30},
        headers=_headers(),
    )
    assert first.status_code == 201

    second = client.post(
        "/process/start",
        json={"command": "sleep 5", "max_runtime_seconds": 30},
        headers=_headers(),
    )
    assert second.status_code == 429

    # Cleanup: stop the process this test started so it doesn't outlive the test.
    client.post(f"/process/{first.json()['process_id']}/stop", headers=_headers())


def test_process_output_reports_stdout_and_exit_code(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _fake_spawn)
    client = _client()

    start = client.post(
        "/process/start",
        json={"command": "echo hello-world", "max_runtime_seconds": 30},
        headers=_headers(),
    )
    process_id = start.json()["process_id"]

    # Poll until the process exits (fast, tiny command) rather than sleeping
    # a fixed amount -- deterministic instead of timing-based.
    for _ in range(200):
        output = client.get(f"/process/{process_id}/output", headers=_headers())
        if output.json()["status"] == "exited":
            break
    else:
        raise AssertionError("process never reported exited status")

    body = output.json()
    assert body["exit_code"] == 0
    assert "hello-world" in body["stdout_chunk"]
    assert body["truncated"] is False
    assert body["next_offset"] == len(body["stdout_chunk"].encode("utf-8"))


def test_process_output_since_offset_returns_only_new_bytes(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _fake_spawn)
    client = _client()

    start = client.post(
        "/process/start",
        json={"command": "printf 'abc'; sleep 0.05; printf 'def'", "max_runtime_seconds": 30},
        headers=_headers(),
    )
    process_id = start.json()["process_id"]

    first_offset = 0
    for _ in range(200):
        first = client.get(
            f"/process/{process_id}/output",
            params={"since_offset": first_offset},
            headers=_headers(),
        )
        if first.json()["stdout_chunk"]:
            break
    else:
        raise AssertionError("never observed any output")

    assert first.json()["stdout_chunk"] == "abc"
    next_offset = first.json()["next_offset"]

    for _ in range(200):
        second = client.get(
            f"/process/{process_id}/output",
            params={"since_offset": next_offset},
            headers=_headers(),
        )
        if second.json()["status"] == "exited":
            break
    else:
        raise AssertionError("process never exited")

    assert second.json()["stdout_chunk"] == "def"


def test_process_output_unknown_id_is_404(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    client = _client()

    response = client.get("/process/proc_doesnotexist/output", headers=_headers())
    assert response.status_code == 404


def test_process_input_writes_to_stdin(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _fake_spawn)
    client = _client()

    start = client.post(
        "/process/start",
        json={"command": "read line; echo \"got:$line\"", "max_runtime_seconds": 30},
        headers=_headers(),
    )
    process_id = start.json()["process_id"]

    write = client.post(
        f"/process/{process_id}/input",
        json={"data": "hello\n"},
        headers=_headers(),
    )
    assert write.status_code == 200
    assert write.json()["bytes_written"] == len(b"hello\n")

    for _ in range(200):
        output = client.get(f"/process/{process_id}/output", headers=_headers())
        if output.json()["status"] == "exited":
            break
    else:
        raise AssertionError("process never exited")

    assert "got:hello" in output.json()["stdout_chunk"]


def test_process_input_to_exited_process_is_409(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _fake_spawn)
    client = _client()

    start = client.post(
        "/process/start",
        json={"command": "true", "max_runtime_seconds": 30},
        headers=_headers(),
    )
    process_id = start.json()["process_id"]

    for _ in range(200):
        output = client.get(f"/process/{process_id}/output", headers=_headers())
        if output.json()["status"] == "exited":
            break
    else:
        raise AssertionError("process never exited")

    write = client.post(
        f"/process/{process_id}/input",
        json={"data": "too-late\n"},
        headers=_headers(),
    )
    assert write.status_code == 409


def test_process_stop_terminates_a_running_process(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _fake_spawn)
    client = _client()

    start = client.post(
        "/process/start",
        json={"command": "sleep 30", "max_runtime_seconds": 60},
        headers=_headers(),
    )
    process_id = start.json()["process_id"]

    stop = client.post(f"/process/{process_id}/stop", headers=_headers())
    assert stop.status_code == 200
    assert stop.json()["status"] == "stopped"
    assert stop.json()["exit_code"] is not None

    listing = client.get("/process", headers=_headers())
    entry = next(p for p in listing.json()["processes"] if p["process_id"] == process_id)
    assert entry["status"] == "stopped"


def test_process_stop_unknown_id_is_404(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    client = _client()

    response = client.post("/process/proc_doesnotexist/stop", headers=_headers())
    assert response.status_code == 404


def test_process_list_reflects_tracked_processes(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _fake_spawn)
    client = _client()

    empty = client.get("/process", headers=_headers())
    assert empty.json()["processes"] == []

    start = client.post(
        "/process/start",
        json={"command": "sleep 5", "description": "test proc", "max_runtime_seconds": 30},
        headers=_headers(),
    )
    process_id = start.json()["process_id"]

    listing = client.get("/process", headers=_headers())
    entries = listing.json()["processes"]
    assert len(entries) == 1
    assert entries[0]["process_id"] == process_id
    assert entries[0]["description"] == "test proc"
    assert entries[0]["status"] == "running"

    client.post(f"/process/{process_id}/stop", headers=_headers())


def test_process_output_ring_buffer_drops_oldest_bytes_and_reports_truncated(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "PROCESS_OUTPUT_MAX_BYTES", 10)
    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _fake_spawn)
    client = _client()

    # Writes 26 bytes total ('a'..'z' each on its own echo) -- comfortably
    # over the 10-byte cap, so the ring buffer must drop the earliest bytes.
    command = "; ".join(f"printf {chr(ord('a') + i)}" for i in range(26))
    start = client.post(
        "/process/start",
        json={"command": command, "max_runtime_seconds": 30},
        headers=_headers(),
    )
    process_id = start.json()["process_id"]

    for _ in range(200):
        output = client.get(
            f"/process/{process_id}/output",
            params={"since_offset": 0},
            headers=_headers(),
        )
        if output.json()["status"] == "exited":
            break
    else:
        raise AssertionError("process never exited")

    body = output.json()
    assert body["truncated"] is True
    assert len(body["stdout_chunk"]) <= 10
    # The tail of the alphabet should have survived; the head was dropped.
    assert body["stdout_chunk"] == "qrstuvwxyz"


async def test_process_watchdog_kills_process_past_max_runtime_seconds(monkeypatch):
    proc = await asyncio.create_subprocess_shell(
        "sleep 30",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    handle = sidecar_main.ProcessHandle(
        process_id="proc_watchdog_test",
        proc=proc,
        command="sleep 30",
        description=None,
        max_runtime_seconds=0,
    )

    await asyncio.wait_for(sidecar_main._process_watchdog(handle), timeout=5)

    assert handle.status == "killed"
    await proc.wait()
    assert proc.returncode is not None


async def test_kill_all_processes_kills_running_and_clears_registry(monkeypatch):
    sidecar_main._process_registry.clear()
    sidecar_main._process_registry_lock = None

    proc = await asyncio.create_subprocess_shell(
        "sleep 30",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    handle = sidecar_main.ProcessHandle(
        process_id="proc_kill_all_test",
        proc=proc,
        command="sleep 30",
        description=None,
        max_runtime_seconds=60,
    )
    handle.reader_task = asyncio.create_task(sidecar_main._process_reader_loop(handle))
    handle.watchdog_task = asyncio.create_task(sidecar_main._process_watchdog(handle))
    sidecar_main._process_registry[handle.process_id] = handle

    killed = await sidecar_main._kill_all_processes()

    assert killed == 1
    assert sidecar_main._process_registry == {}
    await proc.wait()
    assert proc.returncode is not None


def test_configure_kills_tracked_processes_before_wiping(monkeypatch, tmp_path):
    """Regression test for the cross-tenant leak this feature would
    otherwise introduce: /configure (called on pod recycle/claim) must kill
    every tracked background process before acknowledging, so a process
    started by the previous tenant is never observed by the next one."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(sidecar_main, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(sidecar_main, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(sidecar_main, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(sidecar_main, "TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(sidecar_main, "prefetch_files", lambda *a, **k: asyncio.sleep(0, result=[]))
    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _fake_spawn)
    # os.chown requires root outside a real sandbox container; not what this
    # test is about (it's here to verify the process-kill-before-wipe
    # ordering, not directory ownership).
    monkeypatch.setattr(sidecar_main.os, "chown", lambda *a, **k: None)
    client = _client()

    start = client.post(
        "/process/start",
        json={"command": "sleep 30", "max_runtime_seconds": 60},
        headers=_headers(),
    )
    process_id = start.json()["process_id"]
    assert sidecar_main._process_registry  # sanity: something is tracked

    configure = client.post(
        "/configure",
        json={"session_id": None, "organization_id": None, "work_item_id": None},
        headers=_headers(),
    )
    assert configure.status_code == 200
    assert sidecar_main._process_registry == {}

    # The next tenant's session must not see the previous tenant's process.
    listing = client.get("/process", headers=_headers())
    assert listing.json()["processes"] == []
    lookup = client.get(f"/process/{process_id}/output", headers=_headers())
    assert lookup.status_code == 404


async def test_process_kill_all_endpoint(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _fake_spawn)
    client = _client()

    client.post(
        "/process/start",
        json={"command": "sleep 30", "max_runtime_seconds": 60},
        headers=_headers(),
    )

    response = client.post("/process/kill-all", headers=_headers())
    assert response.status_code == 200
    assert response.json()["killed"] == 1
    assert sidecar_main._process_registry == {}
