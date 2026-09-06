"""explain_last_failure: last failed command + what it touched (issue #77).

The two claims worth pinning: a scrubbed secret must not survive in the
failure slot, and the deletions limitation must be stated in every response
rather than left for the agent to discover by trusting an empty list.
"""

import os
import time

import pytest

import main as sidecar_main
import sidecar_last_failure as lastfail


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    ws = tmp_path / "workspace"
    ws.mkdir()
    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(ws))
    lastfail._reset_state_for_tests()
    yield ws
    lastfail._reset_state_for_tests()


async def _explain(**kw):
    return await lastfail.explain_last_failure(
        sidecar_main.ExplainLastFailureRequest(**kw)
    )


def _record(**kw):
    now = time.time()
    defaults = dict(
        command="pytest -q", exit_code=1, stdout="", stderr="boom",
        started_at=now - 1.0, ended_at=now,
    )
    defaults.update(kw)
    lastfail.record_failure(**defaults)


# ── nothing recorded ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reports_no_failure_rather_than_an_empty_one(workspace):
    result = await _explain()

    assert result.found is False
    assert result.command is None
    assert "No command has failed" in result.notes[0]


# ── the recorded failure ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_returns_the_command_exit_code_and_streams(workspace):
    _record(command="make build", exit_code=2, stdout="compiling\n", stderr="error: no rule\n")

    result = await _explain()

    assert result.found is True
    assert result.command == "make build"
    assert result.exit_code == 2
    assert result.stdout == "compiling\n"
    assert result.stderr == "error: no rule\n"
    assert result.duration_seconds == pytest.approx(1.0, abs=0.1)


@pytest.mark.asyncio
async def test_only_the_most_recent_failure_is_kept(workspace):
    _record(command="first", exit_code=1)
    _record(command="second", exit_code=3)

    result = await _explain()

    assert result.command == "second"
    assert result.exit_code == 3


@pytest.mark.asyncio
async def test_oversized_streams_are_capped_and_the_cap_is_announced(workspace):
    _record(stdout="x" * (lastfail.LAST_FAILURE_MAX_STREAM_BYTES + 500), stderr="y")

    result = await _explain()

    assert len(result.stdout) == lastfail.LAST_FAILURE_MAX_STREAM_BYTES
    assert any("stdout was capped" in n for n in result.notes)


# ── touched files ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_files_written_during_the_window_are_reported(workspace):
    start = time.time()
    target = workspace / "out.log"
    target.write_text("partial output\n")
    end = time.time()
    _record(started_at=start, ended_at=end)

    result = await _explain()

    assert [t.path for t in result.touched_files] == [
        os.path.join(os.path.realpath(str(workspace)), "out.log")
    ]
    assert result.touched_files[0].size_bytes == len("partial output\n")


@pytest.mark.asyncio
async def test_files_written_outside_the_window_are_not_attributed(workspace):
    """Attributing an unrelated write to the failing command sends the agent
    to debug the wrong file."""
    old = workspace / "unrelated.txt"
    old.write_text("written long before\n")
    os.utime(old, (time.time() - 3600, time.time() - 3600))

    start = time.time()
    (workspace / "during.txt").write_text("x\n")
    _record(started_at=start, ended_at=time.time())

    result = await _explain()

    paths = [os.path.basename(t.path) for t in result.touched_files]
    assert paths == ["during.txt"]


@pytest.mark.asyncio
async def test_the_deletions_limitation_is_stated_in_the_response(workspace):
    """An empty deletions list that looked authoritative is worse than none.
    The caveat travels with every answer, not just the docs."""
    _record()

    result = await _explain()

    assert any("deleted files cannot be detected" in n for n in result.notes)
    assert any("workspace_diff" in n for n in result.notes)


@pytest.mark.asyncio
async def test_touched_file_scan_can_be_skipped(workspace):
    start = time.time()
    (workspace / "out.log").write_text("x\n")
    _record(started_at=start, ended_at=time.time())

    result = await _explain(include_touched_files=False)

    assert result.touched_files == []
    # No scan ran, so the deletions caveat would be misleading here.
    assert not any("deleted files" in n for n in result.notes)


@pytest.mark.asyncio
async def test_touched_files_are_capped_and_the_cap_is_announced(workspace, monkeypatch):
    monkeypatch.setattr(lastfail, "LAST_FAILURE_MAX_TOUCHED_FILES", 3)
    start = time.time()
    for i in range(10):
        (workspace / f"f{i}.txt").write_text(str(i))
    _record(started_at=start, ended_at=time.time())

    result = await _explain()

    assert len(result.touched_files) == 3
    assert any("the list is capped" in n for n in result.notes)


@pytest.mark.asyncio
async def test_excluded_directories_are_not_scanned(workspace):
    start = time.time()
    (workspace / "node_modules").mkdir()
    (workspace / "node_modules" / "dep.js").write_text("x\n")
    (workspace / "real.txt").write_text("y\n")
    _record(started_at=start, ended_at=time.time())

    result = await _explain()

    assert [os.path.basename(t.path) for t in result.touched_files] == ["real.txt"]


@pytest.mark.asyncio
async def test_symlinks_are_not_followed(workspace, tmp_path):
    secret = tmp_path / "secret.txt"
    secret.write_text("do not read\n")
    start = time.time()
    os.symlink(str(secret), str(workspace / "link.txt"))
    _record(started_at=start, ended_at=time.time())

    result = await _explain()

    assert result.touched_files == []


# ── recording from /exec ──────────────────────────────────────────────────


def _auth_headers():
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def _exec_client(monkeypatch):
    from fastapi.testclient import TestClient

    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    return TestClient(sidecar_main.app)


def test_a_failing_exec_is_recorded(monkeypatch):
    lastfail._reset_state_for_tests()

    async def _fake_exec(command, timeout, extra_env=None):
        return (7, "some output\n", "it broke\n")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec)
    client = _exec_client(monkeypatch)

    client.post("/exec", json={"command": "make", "timeout": 5}, headers=_auth_headers())

    assert lastfail._last_failure["command"] == "make"
    assert lastfail._last_failure["exit_code"] == 7
    assert lastfail._last_failure["stderr"] == "it broke\n"
    lastfail._reset_state_for_tests()


def test_a_successful_exec_is_not_recorded(monkeypatch):
    lastfail._reset_state_for_tests()

    async def _fake_exec(command, timeout, extra_env=None):
        return (0, "fine\n", "")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec)
    client = _exec_client(monkeypatch)

    client.post("/exec", json={"command": "true", "timeout": 5}, headers=_auth_headers())

    assert lastfail._last_failure is None


def test_a_scrubbed_secret_does_not_survive_in_the_recorded_failure(monkeypatch):
    """The ordering that matters. /exec scrubs resolved secret values out of
    stdout/stderr before building its response; recording before that would
    park the raw credential in the failure slot for a later
    explain_last_failure call to hand straight back.
    """
    lastfail._reset_state_for_tests()
    monkeypatch.setitem(sidecar_main.current_session, "secret_names", ["claude-code-key"])
    sidecar_main._secret_value_cache.clear()

    async def _fake_get_secret_value(name):
        return "sk-ant-the-real-value"

    async def _fake_exec(command, timeout, extra_env=None):
        # A failing program that echoes the credential it was handed.
        return (1, "using key sk-ant-the-real-value\n", "failed with sk-ant-the-real-value\n")

    monkeypatch.setattr(sidecar_main, "_get_secret_value", _fake_get_secret_value)
    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec)
    client = _exec_client(monkeypatch)

    response = client.post(
        "/exec",
        json={
            "command": "some-tool",
            "timeout": 5,
            "secret_env": {"ANTHROPIC_API_KEY": "claude-code-key"},
        },
        headers=_auth_headers(),
    )
    assert response.status_code == 200
    assert "sk-ant-the-real-value" not in response.json()["stdout"]

    recorded = lastfail._last_failure
    assert "sk-ant-the-real-value" not in recorded["stdout"]
    assert "sk-ant-the-real-value" not in recorded["stderr"]
    assert "[REDACTED_SECRET:claude-code-key]" in recorded["stdout"]
    lastfail._reset_state_for_tests()
