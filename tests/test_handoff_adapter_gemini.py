from pathlib import Path
import pytest

from boxxkite_handoff.adapters.gemini import GeminiAdapter
from boxxkite_handoff.core import HandoffError


def test_gemini_adapter_no_directory(tmp_path: Path):
    adapter = GeminiAdapter(gemini_home=tmp_path / "non_existent")
    with pytest.raises(HandoffError, match="No Gemini sessions directory found"):
        adapter.locate_session()


def test_gemini_adapter_locate_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")

    sessions_dir = tmp_path / "tmp"
    sessions_dir.mkdir(parents=True)

    session_file = sessions_dir / "test-session.json"
    session_file.write_text('{"history": []}')

    adapter = GeminiAdapter(gemini_home=tmp_path)
    located = adapter.locate_session()

    assert located.tool == "gemini"
    assert located.session_id == "test-session"
    assert located.credential.env_var == "GEMINI_API_KEY"
    assert located.credential.value == "test-key-123"
    assert located.resume_command == "gemini --resume test-session"
