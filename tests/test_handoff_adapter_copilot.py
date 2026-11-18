import os
from pathlib import Path

import pytest

from boxxkite.handoff.adapters.copilot import CopilotAdapter
from boxxkite.handoff.core import HandoffError


SESSION_A = "12345678-1234-4234-8234-123456789abc"
SESSION_B = "b91dba6b-2c28-4355-a922-c552638877b2"
TOKEN = "github_pat_test"


def _write_session(home: Path, session_dir: str) -> Path:
    events = home / "session-state" / session_dir / "events.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text('{"type":"session.start","data":{}}\n', encoding="utf-8")
    return events


def test_locates_latest_session_and_maps_to_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", TOKEN)
    older = _write_session(tmp_path, SESSION_A)
    newer = _write_session(tmp_path, SESSION_B)
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    located = CopilotAdapter(copilot_home=tmp_path).locate_session()

    assert located.tool == "copilot"
    assert located.session_id == SESSION_B
    assert located.files[0].local_path == newer
    assert located.files[0].sandbox_path == (
        f"/workspace/.copilot/session-state/{SESSION_B}/events.jsonl"
    )
    assert located.resume_command == f"copilot --resume {SESSION_B}"
    assert located.workdir == "/workspace"
    assert located.credential.env_var == "COPILOT_GITHUB_TOKEN"
    assert located.credential.value == TOKEN


def test_selects_explicit_older_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", TOKEN)
    older = _write_session(tmp_path, SESSION_A)
    newer = _write_session(tmp_path, SESSION_B)
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    located = CopilotAdapter(copilot_home=tmp_path).locate_session(session_ref=SESSION_A.upper())

    assert located.session_id == SESSION_A
    assert located.files[0].local_path == older


def test_ignores_non_uuid_session_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", TOKEN)
    _write_session(tmp_path, "x; echo injected")

    with pytest.raises(HandoffError, match="No resumable"):
        CopilotAdapter(copilot_home=tmp_path).locate_session()


def test_rejects_invalid_session_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", TOKEN)
    _write_session(tmp_path, SESSION_A)

    with pytest.raises(HandoffError, match="not a valid UUID"):
        CopilotAdapter(copilot_home=tmp_path).locate_session(session_ref="latest")


def test_rejects_unknown_session_ref(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", TOKEN)
    _write_session(tmp_path, SESSION_A)

    with pytest.raises(HandoffError, match="No local GitHub Copilot CLI session found"):
        CopilotAdapter(copilot_home=tmp_path).locate_session(session_ref=SESSION_B)


def test_requires_copilot_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("COPILOT_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GH_TOKEN", "gho_broad")
    _write_session(tmp_path, SESSION_A)

    with pytest.raises(HandoffError, match="COPILOT_GITHUB_TOKEN is not set"):
        CopilotAdapter(copilot_home=tmp_path).locate_session()


def test_rejects_classic_pat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", "ghp_classic")
    _write_session(tmp_path, SESSION_A)

    with pytest.raises(HandoffError, match="classic personal access token"):
        CopilotAdapter(copilot_home=tmp_path).locate_session()


def test_honors_copilot_home_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("COPILOT_GITHUB_TOKEN", TOKEN)
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path))
    events = _write_session(tmp_path, SESSION_A)

    located = CopilotAdapter().locate_session()

    assert located.files[0].local_path == events
