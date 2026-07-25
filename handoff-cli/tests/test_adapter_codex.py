"""Tests for the Codex CLI (openai/codex) handoff adapter.

Session-file layout, resume mechanism, and credential resolution here are all
based on directly reading openai/codex's own source (rollout file naming in
codex-rs/rollout/src/recorder.rs, `codex resume <id>` lookup in
codex-rs/rollout/src/list.rs, CODEX_HOME resolution in
codex-rs/utils/home-dir/src/lib.rs, and auth.json's schema plus
OPENAI_API_KEY/CODEX_API_KEY/CODEX_ACCESS_TOKEN env vars in
codex-rs/login/src/auth/manager.rs) -- not assumed from the task prompt.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from boxkite_handoff.adapters.codex import CodexAdapter
from boxkite_handoff.core import HandoffError

UUID_OLD = "5973b6c0-94b8-487b-a530-2aeb6098ae0e"
UUID_NEW = "123e4567-e89b-12d3-a456-426614174000"


def _write_rollout(
    codex_home: Path,
    *,
    year: str,
    month: str,
    day: str,
    timestamp: str,
    session_uuid: str,
    body: str = '{"role": "user", "content": "hello"}\n',
) -> Path:
    day_dir = codex_home / "sessions" / year / month / day
    day_dir.mkdir(parents=True, exist_ok=True)
    path = day_dir / f"rollout-{timestamp}-{session_uuid}.jsonl"
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _clear_codex_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_HOME"):
        monkeypatch.delenv(var, raising=False)


@pytest.fixture
def codex_home(tmp_path: Path) -> Path:
    home = tmp_path / "codex-home"
    home.mkdir()
    return home


def test_name_attribute_is_codex() -> None:
    assert CodexAdapter.name == "codex"


def test_locate_session_raises_when_sessions_dir_missing(codex_home: Path) -> None:
    adapter = CodexAdapter(codex_home=codex_home)

    with pytest.raises(HandoffError):
        adapter.locate_session()


def test_locate_session_raises_when_no_rollout_files_present(codex_home: Path) -> None:
    (codex_home / "sessions").mkdir()
    adapter = CodexAdapter(codex_home=codex_home)

    with pytest.raises(HandoffError):
        adapter.locate_session()


def test_locate_session_picks_most_recently_modified_rollout_file(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    older = _write_rollout(
        codex_home,
        year="2025",
        month="09",
        day="12",
        timestamp="2025-09-12T16-41-03",
        session_uuid=UUID_OLD,
    )
    newer = _write_rollout(
        codex_home,
        year="2026",
        month="07",
        day="20",
        timestamp="2026-07-20T09-00-00",
        session_uuid=UUID_NEW,
    )
    # Ensure the mtime ordering is unambiguous regardless of write speed.
    older_stat = older.stat()
    import os

    os.utime(older, (older_stat.st_atime - 1000, older_stat.st_mtime - 1000))

    adapter = CodexAdapter(codex_home=codex_home)
    located = adapter.locate_session()

    assert located.session_id == UUID_NEW
    assert located.files[0].local_path == newer


def test_locate_session_by_explicit_session_ref(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _write_rollout(
        codex_home,
        year="2025",
        month="09",
        day="12",
        timestamp="2025-09-12T16-41-03",
        session_uuid=UUID_OLD,
    )
    _write_rollout(
        codex_home,
        year="2026",
        month="07",
        day="20",
        timestamp="2026-07-20T09-00-00",
        session_uuid=UUID_NEW,
    )

    adapter = CodexAdapter(codex_home=codex_home)
    located = adapter.locate_session(session_ref=UUID_OLD)

    assert located.session_id == UUID_OLD


def test_locate_session_raises_when_session_ref_not_found(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _write_rollout(
        codex_home,
        year="2026",
        month="07",
        day="20",
        timestamp="2026-07-20T09-00-00",
        session_uuid=UUID_NEW,
    )
    adapter = CodexAdapter(codex_home=codex_home)

    with pytest.raises(HandoffError):
        adapter.locate_session(session_ref="00000000-0000-0000-0000-000000000000")


def test_locate_session_ignores_compressed_zst_rollout_files(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    day_dir = codex_home / "sessions" / "2026" / "07" / "20"
    day_dir.mkdir(parents=True)
    (day_dir / f"rollout-2026-07-20T09-00-00-{UUID_NEW}.jsonl.zst").write_bytes(b"not-plain-jsonl")
    adapter = CodexAdapter(codex_home=codex_home)

    with pytest.raises(HandoffError):
        adapter.locate_session()


def test_located_session_sandbox_path_mirrors_codex_home_layout(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _write_rollout(
        codex_home,
        year="2026",
        month="07",
        day="20",
        timestamp="2026-07-20T09-00-00",
        session_uuid=UUID_NEW,
    )
    adapter = CodexAdapter(codex_home=codex_home)

    located = adapter.locate_session()

    assert located.files[0].sandbox_path == (
        f"/workspace/.codex/sessions/2026/07/20/rollout-2026-07-20T09-00-00-{UUID_NEW}.jsonl"
    )


def test_located_session_resume_command_and_workdir(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _write_rollout(
        codex_home,
        year="2026",
        month="07",
        day="20",
        timestamp="2026-07-20T09-00-00",
        session_uuid=UUID_NEW,
    )
    adapter = CodexAdapter(codex_home=codex_home)

    located = adapter.locate_session()

    assert located.resume_command == f"codex resume {UUID_NEW}"
    assert located.workdir == "/workspace"
    assert located.tool == "codex"


def test_credential_prefers_openai_api_key_env_var(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-env-value")
    monkeypatch.setenv("CODEX_API_KEY", "should-not-be-used")
    _write_rollout(
        codex_home,
        year="2026",
        month="07",
        day="20",
        timestamp="2026-07-20T09-00-00",
        session_uuid=UUID_NEW,
    )
    adapter = CodexAdapter(codex_home=codex_home)

    located = adapter.locate_session()

    assert located.credential.env_var == "OPENAI_API_KEY"
    assert located.credential.value == "sk-env-value"


def test_credential_falls_back_to_codex_api_key_env_var(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_API_KEY", "codex-key-value")
    _write_rollout(
        codex_home,
        year="2026",
        month="07",
        day="20",
        timestamp="2026-07-20T09-00-00",
        session_uuid=UUID_NEW,
    )
    adapter = CodexAdapter(codex_home=codex_home)

    located = adapter.locate_session()

    assert located.credential.env_var == "CODEX_API_KEY"
    assert located.credential.value == "codex-key-value"


def test_credential_falls_back_to_codex_access_token_env_var(
    codex_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CODEX_ACCESS_TOKEN", "at-personal-token")
    _write_rollout(
        codex_home,
        year="2026",
        month="07",
        day="20",
        timestamp="2026-07-20T09-00-00",
        session_uuid=UUID_NEW,
    )
    adapter = CodexAdapter(codex_home=codex_home)

    located = adapter.locate_session()

    assert located.credential.env_var == "CODEX_ACCESS_TOKEN"
    assert located.credential.value == "at-personal-token"


def test_credential_falls_back_to_auth_json_personal_access_token(
    codex_home: Path,
) -> None:
    (codex_home / "auth.json").write_text(
        json.dumps({"personal_access_token": "at-from-file"}), encoding="utf-8"
    )
    _write_rollout(
        codex_home,
        year="2026",
        month="07",
        day="20",
        timestamp="2026-07-20T09-00-00",
        session_uuid=UUID_NEW,
    )
    adapter = CodexAdapter(codex_home=codex_home)

    located = adapter.locate_session()

    assert located.credential.env_var == "CODEX_ACCESS_TOKEN"
    assert located.credential.value == "at-from-file"


def test_credential_falls_back_to_auth_json_openai_api_key_field(
    codex_home: Path,
) -> None:
    (codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": "sk-from-file"}), encoding="utf-8"
    )
    _write_rollout(
        codex_home,
        year="2026",
        month="07",
        day="20",
        timestamp="2026-07-20T09-00-00",
        session_uuid=UUID_NEW,
    )
    adapter = CodexAdapter(codex_home=codex_home)

    located = adapter.locate_session()

    assert located.credential.env_var == "OPENAI_API_KEY"
    assert located.credential.value == "sk-from-file"


def test_credential_raises_clear_error_for_chatgpt_oauth_only_auth_json(
    codex_home: Path,
) -> None:
    (codex_home / "auth.json").write_text(
        json.dumps({"tokens": {"access_token": "raw-oauth-access-token"}}),
        encoding="utf-8",
    )
    _write_rollout(
        codex_home,
        year="2026",
        month="07",
        day="20",
        timestamp="2026-07-20T09-00-00",
        session_uuid=UUID_NEW,
    )
    adapter = CodexAdapter(codex_home=codex_home)

    with pytest.raises(HandoffError, match="not.*portable|ChatGPT"):
        adapter.locate_session()


def test_credential_raises_when_nothing_available(codex_home: Path) -> None:
    _write_rollout(
        codex_home,
        year="2026",
        month="07",
        day="20",
        timestamp="2026-07-20T09-00-00",
        session_uuid=UUID_NEW,
    )
    adapter = CodexAdapter(codex_home=codex_home)

    with pytest.raises(HandoffError):
        adapter.locate_session()


def test_default_codex_home_honors_codex_home_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    custom_home = tmp_path / "custom-codex-home"
    custom_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(custom_home))
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    _write_rollout(
        custom_home,
        year="2026",
        month="07",
        day="20",
        timestamp="2026-07-20T09-00-00",
        session_uuid=UUID_NEW,
    )

    adapter = CodexAdapter()
    located = adapter.locate_session()

    assert located.session_id == UUID_NEW
