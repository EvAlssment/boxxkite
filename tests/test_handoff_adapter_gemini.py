from pathlib import Path
import pytest

from boxxkite.handoff.adapters.gemini import GeminiAdapter


def test_gemini_adapter_locate_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-123")

    chats_dir = tmp_path / "tmp" / "my-project" / "chats"
    chats_dir.mkdir(parents=True)

    session_file = chats_dir / "session-abc12345.jsonl"
    session_content = (
        '{"sessionId": "12345678-1234-1234-1234-123456789abc", "role": "user", "message": "hello"}\n'
        '{"sessionId": "12345678-1234-1234-1234-123456789abc", "role": "assistant", "message": "hi"}\n'
    )
    session_file.write_text(session_content, encoding="utf-8")

    adapter = GeminiAdapter(gemini_home=tmp_path)
    located = adapter.locate_session()

    assert located.tool == "gemini"
    assert located.session_id == "12345678-1234-1234-1234-123456789abc"
    assert located.credential.value == "test-key-123"
    assert located.resume_command == "gemini --resume 12345678-1234-1234-1234-123456789abc"


def test_gemini_adapter_no_sessions(tmp_path: Path):
    adapter = GeminiAdapter(gemini_home=tmp_path)
    with pytest.raises(Exception):
        adapter.locate_session()
