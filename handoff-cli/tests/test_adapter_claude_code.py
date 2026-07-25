from __future__ import annotations

import time
from pathlib import Path

import pytest

from boxkite_handoff.adapters.claude_code import ClaudeCodeAdapter, encode_project_dir
from boxkite_handoff.core import HandoffError

SANDBOX_HOME_ENCODED = "-workspace"


def _write_session(project_dir: Path, session_id: str, *, mtime: float | None = None) -> Path:
    project_dir.mkdir(parents=True, exist_ok=True)
    p = project_dir / f"{session_id}.jsonl"
    p.write_text('{"type":"user","message":{"role":"user","content":"hi"}}\n', encoding="utf-8")
    if mtime is not None:
        import os

        os.utime(p, (mtime, mtime))
    return p


@pytest.fixture(autouse=True)
def oauth_token(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-portable-token")


@pytest.fixture
def config_dir(tmp_path: Path) -> Path:
    return tmp_path / "claude-home"


@pytest.fixture
def cwd(tmp_path: Path) -> Path:
    d = tmp_path / "myproject"
    d.mkdir()
    return d


def _adapter(config_dir: Path, cwd: Path) -> ClaudeCodeAdapter:
    return ClaudeCodeAdapter(config_dir=config_dir, cwd=cwd)


class TestEncodeProjectDir:
    def test_replaces_slashes_with_dashes(self) -> None:
        assert encode_project_dir("/Users/dev/Desktop/boxkite") == "-Users-dev-Desktop-boxkite"

    def test_replaces_dots_with_dashes(self) -> None:
        assert encode_project_dir("/Users/h/.hidden.dir/sub") == "-Users-h--hidden-dir-sub"

    def test_encodes_workspace_root(self) -> None:
        assert encode_project_dir("/workspace") == SANDBOX_HOME_ENCODED


class TestLocateSessionMostRecent:
    def test_picks_most_recently_modified_session(self, config_dir: Path, cwd: Path) -> None:
        project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
        _write_session(project_dir, "older-session", mtime=time.time() - 100)
        _write_session(project_dir, "newer-session", mtime=time.time())

        located = _adapter(config_dir, cwd).locate_session()

        assert located.session_id == "newer-session"

    def test_ignores_orphaned_session_files(self, config_dir: Path, cwd: Path) -> None:
        project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
        _write_session(project_dir, "real-session", mtime=time.time() - 100)
        orphaned = project_dir / "real-session.orphaned-123-abcdef.jsonl"
        orphaned.write_text("{}\n", encoding="utf-8")
        import os

        os.utime(orphaned, (time.time(), time.time()))

        located = _adapter(config_dir, cwd).locate_session()

        assert located.session_id == "real-session"

    def test_raises_handoff_error_when_no_project_dir_exists(self, config_dir: Path, cwd: Path) -> None:
        with pytest.raises(HandoffError):
            _adapter(config_dir, cwd).locate_session()

    def test_raises_handoff_error_when_project_dir_has_no_sessions(self, config_dir: Path, cwd: Path) -> None:
        project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
        project_dir.mkdir(parents=True)

        with pytest.raises(HandoffError):
            _adapter(config_dir, cwd).locate_session()

    def test_rejects_a_maliciously_named_session_file_discovered_as_most_recent(
        self, config_dir: Path, cwd: Path
    ) -> None:
        """Regression test for a real command-injection finding:
        auto-discovery picks the newest-mtime .jsonl file's stem as the
        session id, which later flows unquoted into resume_command. A
        locally-planted file with a shell-metacharacter-laden name (a
        compromised local machine is this package's own stated threat
        model) must be rejected here, not silently accepted."""
        project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
        _write_session(project_dir, "safe-session", mtime=time.time() - 100)
        _write_session(project_dir, "x'; touch pwned #", mtime=time.time())

        with pytest.raises(HandoffError):
            _adapter(config_dir, cwd).locate_session()


class TestLocateSessionBySessionRef:
    def test_finds_exact_session_by_id(self, config_dir: Path, cwd: Path) -> None:
        project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
        _write_session(project_dir, "session-a")
        _write_session(project_dir, "session-b")

        located = _adapter(config_dir, cwd).locate_session(session_ref="session-a")

        assert located.session_id == "session-a"

    def test_raises_handoff_error_for_unknown_session_ref(self, config_dir: Path, cwd: Path) -> None:
        project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
        _write_session(project_dir, "session-a")

        with pytest.raises(HandoffError):
            _adapter(config_dir, cwd).locate_session(session_ref="does-not-exist")

    def test_rejects_a_malicious_explicit_session_ref(self, config_dir: Path, cwd: Path) -> None:
        with pytest.raises(HandoffError):
            _adapter(config_dir, cwd).locate_session(session_ref="x'; touch pwned #")


class TestLocatedSessionShape:
    def test_maps_session_file_to_sandbox_workspace_project_dir(self, config_dir: Path, cwd: Path) -> None:
        project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
        _write_session(project_dir, "abc123")

        located = _adapter(config_dir, cwd).locate_session()

        assert len(located.files) == 1
        f = located.files[0]
        assert f.local_path == project_dir / "abc123.jsonl"
        assert f.sandbox_path == f"/workspace/.claude/projects/{SANDBOX_HOME_ENCODED}/abc123.jsonl"

    def test_workdir_is_the_sandbox_home(self, config_dir: Path, cwd: Path) -> None:
        project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
        _write_session(project_dir, "abc123")

        located = _adapter(config_dir, cwd).locate_session()

        assert located.workdir == "/workspace"

    def test_resume_command_uses_session_id(self, config_dir: Path, cwd: Path) -> None:
        project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
        _write_session(project_dir, "abc123")

        located = _adapter(config_dir, cwd).locate_session()

        assert located.resume_command == "claude --resume abc123"

    def test_tool_name_is_claude_code(self, config_dir: Path, cwd: Path) -> None:
        project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
        _write_session(project_dir, "abc123")

        located = _adapter(config_dir, cwd).locate_session()

        assert located.tool == "claude-code"


class TestCredential:
    def test_uses_claude_code_oauth_token_env_var(self, config_dir: Path, cwd: Path, monkeypatch) -> None:
        project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
        _write_session(project_dir, "abc123")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-real-token")

        located = _adapter(config_dir, cwd).locate_session()

        assert located.credential.env_var == "CLAUDE_CODE_OAUTH_TOKEN"
        assert located.credential.value == "sk-real-token"

    def test_raises_handoff_error_when_oauth_token_missing(self, config_dir: Path, cwd: Path, monkeypatch) -> None:
        project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
        _write_session(project_dir, "abc123")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        with pytest.raises(HandoffError, match="setup-token"):
            _adapter(config_dir, cwd).locate_session()

    def test_does_not_check_for_credential_before_finding_a_session(
        self, config_dir: Path, cwd: Path, monkeypatch
    ) -> None:
        """No session exists and no token is set -- the session-not-found error
        should win, since a HandoffError about a missing token would be
        misleading when there is nothing to hand off in the first place."""
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

        with pytest.raises(HandoffError, match="session"):
            _adapter(config_dir, cwd).locate_session()


class TestDefaultConfigDirResolution:
    def test_honors_claude_config_dir_env_var_when_not_explicitly_overridden(
        self, tmp_path: Path, cwd: Path, monkeypatch
    ) -> None:
        env_config_dir = tmp_path / "env-claude-home"
        project_dir = env_config_dir / "projects" / encode_project_dir(str(cwd))
        _write_session(project_dir, "abc123")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(env_config_dir))

        located = ClaudeCodeAdapter(cwd=cwd).locate_session()

        assert located.session_id == "abc123"
