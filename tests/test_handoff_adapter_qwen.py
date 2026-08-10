import os
from pathlib import Path

import pytest

from boxxkite.handoff.adapters.qwen import QwenAdapter
from boxxkite.handoff.core import HandoffError


SESSION_A = "12345678-1234-4234-8234-123456789abc"
SESSION_B = "87654321-4321-4321-8321-cba987654321"


def _write_session(root: Path, project: str, session_id: str) -> Path:
    chats = root / "projects" / project / "chats"
    chats.mkdir(parents=True, exist_ok=True)
    session = chats / f"{session_id}.jsonl"
    session.write_text(
        '{"sessionId":"' + session_id + '","type":"user","message":"hello","cwd":"/original/project"}\n',
        encoding="utf-8",
    )
    return session


def test_locates_latest_session_and_maps_to_sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    older = _write_session(tmp_path, "-old-project", SESSION_A)
    newer = _write_session(tmp_path, "-current-project", SESSION_B)
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    located = QwenAdapter(qwen_runtime_dir=tmp_path).locate_session()

    assert located.tool == "qwen"
    assert located.session_id == SESSION_B
    assert located.files[0].local_path != newer
    assert '"cwd":"/workspace"' in located.files[0].local_path.read_text(encoding="utf-8")
    assert '"cwd":"/original/project"' in newer.read_text(encoding="utf-8")
    assert located.files[0].sandbox_path == (
        f"/workspace/.qwen/projects/-workspace/chats/{SESSION_B}.jsonl"
    )
    assert located.credential.env_var == "DASHSCOPE_API_KEY"
    assert located.resume_command == f"qwen --resume {SESSION_B}"

    rewritten = located.files[0].local_path
    assert located.cleanup is not None
    located.cleanup()
    assert not rewritten.exists()


def test_selects_explicit_session_across_projects(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "compatible-key")
    selected = _write_session(tmp_path, "-one", SESSION_A)
    _write_session(tmp_path, "-two", SESSION_B)

    located = QwenAdapter(qwen_runtime_dir=tmp_path).locate_session(session_ref=SESSION_A)

    assert located.files[0].local_path != selected
    assert '"cwd":"/workspace"' in located.files[0].local_path.read_text(encoding="utf-8")
    assert located.credential.env_var == "OPENAI_API_KEY"


def test_rejects_non_uuid_session_record(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "test-key")
    chats = tmp_path / "projects" / "-project" / "chats"
    chats.mkdir(parents=True)
    (chats / "planted.jsonl").write_text(
        '{"sessionId":"bad; echo injected","type":"user"}\n', encoding="utf-8"
    )

    with pytest.raises(HandoffError, match="No resumable"):
        QwenAdapter(qwen_runtime_dir=tmp_path).locate_session()


def test_requires_portable_credential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _write_session(tmp_path, "-project", SESSION_A)

    with pytest.raises(HandoffError, match="No portable Qwen credential"):
        QwenAdapter(qwen_runtime_dir=tmp_path).locate_session()
