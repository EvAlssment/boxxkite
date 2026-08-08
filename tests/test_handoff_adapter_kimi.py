from pathlib import Path
import pytest

from boxxkite.handoff.adapters.kimi import KimiAdapter
from boxxkite.handoff.core import HandoffError


def create_mock_kimi_env(kimi_home: Path) -> None:
    """Helper to setup base Kimi files."""
    kimi_home.mkdir(parents=True, exist_ok=True)
    config = kimi_home / "config.toml"
    config.write_text('[providers."managed:kimi-code"]\napi_key = "test-key-123"')


def test_kimi_adapter_locate_latest_session(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    create_mock_kimi_env(kimi_home)

    # Nest inside a project bucket folder as per Kimi 0.34.0 behavior
    session_dir = kimi_home / "sessions" / "myproject_1a2b3c" / "session-uuid-123"
    session_dir.mkdir(parents=True)

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session()

    assert located.tool == "kimi"
    assert located.session_id == "session-uuid-123"
    assert located.resume_command == "kimi --session session-uuid-123"
    # Ensure the bucket name is preserved in the sandbox path
    assert "myproject_1a2b3c" in located.files[0].sandbox_path
    assert located.credential.value == "test-key-123"


def test_kimi_adapter_locate_by_id(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    create_mock_kimi_env(kimi_home)

    (kimi_home / "sessions" / "bucketA" / "session-1").mkdir(parents=True)
    (kimi_home / "sessions" / "bucketB" / "session-2").mkdir(parents=True)

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session(session_ref="session-2")

    assert located.session_id == "session-2"


def test_kimi_adapter_missing_dir_raises(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    create_mock_kimi_env(kimi_home)

    adapter = KimiAdapter(kimi_home=kimi_home)
    with pytest.raises(HandoffError, match="No Kimi sessions directory found"):
        adapter.locate_session()


def test_kimi_adapter_empty_sessions_dir_raises(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    create_mock_kimi_env(kimi_home)
    (kimi_home / "sessions").mkdir(parents=True)

    adapter = KimiAdapter(kimi_home=kimi_home)
    with pytest.raises(HandoffError, match="No local Kimi sessions found"):
        adapter.locate_session()


def test_kimi_adapter_missing_config_raises(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    (kimi_home / "sessions" / "bucket" / "sess-1").mkdir(parents=True)

    adapter = KimiAdapter(kimi_home=kimi_home)
    with pytest.raises(HandoffError, match="No Kimi config found"):
        adapter.locate_session()
