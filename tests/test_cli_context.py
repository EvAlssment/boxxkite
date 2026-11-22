from __future__ import annotations

import os
from pathlib import Path

import pytest
from test_handoff_context import (
    CLAUDE_SESSION_ID,
    CODEX_RECORDS,
    CODEX_SESSION_ID,
    _claude_assistant,
    _claude_user,
    _write_jsonl,
)
from typer.testing import CliRunner

from boxxkite.cli import app, config_store
from boxxkite.handoff.adapters.claude_code import encode_project_dir
from boxxkite.handoff.context import artifact_from_json

runner = CliRunner()

OLDER_SESSION_ID = "99999999-8888-7777-6666-555555555555"
CLAUDE_MESSAGES = [
    ("user", "Fix the flaky login test."),
    ("assistant", "The test races the session cookie."),
    ("user", "Add a retry."),
    ("assistant", "Done."),
]


@pytest.fixture(autouse=True)
def _no_boxxkite_config(tmp_path, monkeypatch):
    config_dir = tmp_path / ".boxxkite"
    monkeypatch.setattr(config_store, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(config_store, "LOCAL_ENV_FILE", config_dir / "local.env")


def _claude_records(messages: list[tuple[str, str]]) -> list[dict]:
    return [
        _claude_user(text) if role == "user" else _claude_assistant({"type": "text", "text": text})
        for role, text in messages
    ]


@pytest.fixture
def claude_project(tmp_path: Path, monkeypatch) -> Path:
    config_dir, cwd = tmp_path / "claude", (tmp_path / "project").resolve()
    cwd.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    monkeypatch.chdir(cwd)
    project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
    _write_jsonl(project_dir / f"{CLAUDE_SESSION_ID}.jsonl", _claude_records(CLAUDE_MESSAGES))
    return project_dir


def _pairs(stdout: str) -> list[tuple[str, str]]:
    return [(m.role, m.text) for m in artifact_from_json(stdout).messages]


def test_export_prints_a_json_artifact_that_round_trips(claude_project) -> None:
    result = runner.invoke(app, ["context", "export", "claude-code"])

    assert result.exit_code == 0, result.output
    artifact = artifact_from_json(result.stdout)
    assert artifact.source_tool == "claude-code"
    assert artifact.source_session_id == CLAUDE_SESSION_ID
    assert _pairs(result.stdout) == CLAUDE_MESSAGES


def test_export_reads_codex_rollouts_from_codex_home(tmp_path: Path, monkeypatch) -> None:
    codex_home = tmp_path / "codex"
    _write_jsonl(
        codex_home / "sessions/2026/11/16" / f"rollout-2026-11-16T12-52-24-{CODEX_SESSION_ID}.jsonl",
        CODEX_RECORDS,
    )
    monkeypatch.setenv("CODEX_HOME", str(codex_home))

    result = runner.invoke(app, ["context", "export", "codex"])

    assert result.exit_code == 0, result.output
    assert artifact_from_json(result.stdout).source_session_id == CODEX_SESSION_ID
    assert _pairs(result.stdout) == [
        ("user", "Remember PELICAN-42"),
        ("assistant", "Noted."),
        ("user", "What was the code?"),
        ("assistant", "PELICAN-42."),
    ]


def test_session_option_selects_a_session_other_than_the_most_recent(claude_project) -> None:
    older = _write_jsonl(
        claude_project / f"{OLDER_SESSION_ID}.jsonl", _claude_records([("user", "An older request.")])
    )
    os.utime(older, (1_000_000, 1_000_000))

    result = runner.invoke(app, ["context", "export", "claude-code", "--session", OLDER_SESSION_ID])

    assert result.exit_code == 0, result.output
    assert artifact_from_json(result.stdout).source_session_id == OLDER_SESSION_ID
    assert _pairs(result.stdout) == [("user", "An older request.")]


def test_last_keeps_the_newest_messages_and_records_the_omission(claude_project) -> None:
    result = runner.invoke(app, ["context", "export", "claude-code", "--last", "1"])

    assert result.exit_code == 0, result.output
    assert _pairs(result.stdout) == [("assistant", "Done.")]
    assert "3 earlier messages" in artifact_from_json(result.stdout).omitted


def test_prompt_prints_a_prompt_instead_of_json(claude_project) -> None:
    result = runner.invoke(app, ["context", "export", "claude-code", "--prompt"])

    assert result.exit_code == 0, result.output
    assert result.stdout.startswith(f"Context from a claude-code session ({CLAUDE_SESSION_ID}).")
    assert '{"role": "user", "text": "Fix the flaky login test."}' in result.stdout
    assert "BEGIN TRANSCRIPT" in result.stdout


def test_max_chars_caps_the_prompt_by_dropping_the_oldest_messages(claude_project) -> None:
    full = runner.invoke(app, ["context", "export", "claude-code", "--prompt"]).stdout.rstrip("\n")
    limit = len(full) - 1

    result = runner.invoke(app, ["context", "export", "claude-code", "--prompt", "--max-chars", str(limit)])

    assert result.exit_code == 0, result.output
    prompt = result.stdout.rstrip("\n")
    assert len(prompt) <= limit
    assert "oldest messages were dropped" in prompt
    assert "Fix the flaky login test." not in prompt


def test_max_chars_without_prompt_is_an_error(claude_project) -> None:
    result = runner.invoke(app, ["context", "export", "claude-code", "--max-chars", "1000"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert "Error:" in result.stderr
    assert "--prompt" in result.stderr


def test_unknown_tool_lists_the_supported_tools() -> None:
    result = runner.invoke(app, ["context", "export", "not-a-real-tool"])

    assert result.exit_code == 1
    assert "Supported tools: claude-code, codex." in result.stderr


def test_missing_session_is_a_clean_error(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "empty-claude"))
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["context", "export", "claude-code"])

    assert result.exit_code == 1
    assert result.stdout == ""
    assert result.stderr.startswith("Error: No Claude Code session directory found")
    assert "Traceback" not in result.output
