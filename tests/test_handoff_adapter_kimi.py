from pathlib import Path
import pytest

from boxxkite.handoff.adapters.kimi import KimiAdapter
from boxxkite.handoff.core import HandoffError


def test_kimi_adapter_locate_latest_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KIMI_API_KEY", "test-key-123")

    kimi_home = tmp_path / ".kimi-code"
    session_dir = kimi_home / "sessions" / "session-uuid-123"
    session_dir.mkdir(parents=True)

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session()

    assert located.tool == "kimi"
    assert located.session_id == "session-uuid-123"
    assert located.resume_command == "kimi --session session-uuid-123"
    assert located.credential.env_var == "KIMI_API_KEY"
    assert located.credential.value == "test-key-123"


def test_kimi_adapter_locate_by_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("KIMI_API_KEY", "test-key-123")

    kimi_home = tmp_path / ".kimi-code"
    (kimi_home / "sessions" / "session-1").mkdir(parents=True)
    (kimi_home / "sessions" / "session-2").mkdir(parents=True)

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session(session_ref="session-1")

    assert located.session_id == "session-1"


def test_kimi_adapter_missing_dir_raises(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"

    adapter = KimiAdapter(kimi_home=kimi_home)
    with pytest.raises(HandoffError, match="No Kimi sessions directory found"):
        adapter.locate_session()


def test_kimi_adapter_empty_sessions_dir_raises(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    (kimi_home / "sessions").mkdir(parents=True)

    adapter = KimiAdapter(kimi_home=kimi_home)
    with pytest.raises(HandoffError, match="No local Kimi sessions found"):
        adapter.locate_session()
