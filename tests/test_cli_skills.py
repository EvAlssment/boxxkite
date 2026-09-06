"""Tests for the packaged Claude Code and Cursor troubleshooting skill."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from typer.testing import CliRunner

from boxxkite.cli import app
from boxxkite.skill_assets import troubleshoot_sandbox

runner = CliRunner()
REPO_ROOT = Path(__file__).parents[1]


def test_packaged_skill_assets_match_published_source_files() -> None:
    package_root = files(troubleshoot_sandbox)
    assert package_root.joinpath("SKILL.md").read_text(encoding="utf-8") == (
        REPO_ROOT / "skills/troubleshoot-sandbox/SKILL.md"
    ).read_text(encoding="utf-8")
    assert package_root.joinpath("troubleshoot-sandbox.mdc").read_text(encoding="utf-8") == (
        REPO_ROOT / ".cursor/rules/troubleshoot-sandbox.mdc"
    ).read_text(encoding="utf-8")


def test_skills_install_writes_project_local_claude_skill(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["skills", "install", "claude-code"])

    assert result.exit_code == 0, result.output
    destination = tmp_path / ".claude/skills/troubleshoot-sandbox/SKILL.md"
    content = destination.read_text(encoding="utf-8")
    assert content.startswith("---\nname: troubleshoot-sandbox")
    assert "Do not ask the user to paste an API" in content
    assert "/v1/sandboxes/<SESSION_ID>/diagnostics/summary" in content


def test_skills_install_writes_project_local_cursor_rule(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["skills", "install", "cursor"])

    assert result.exit_code == 0, result.output
    destination = tmp_path / ".cursor/rules/troubleshoot-sandbox.mdc"
    content = destination.read_text(encoding="utf-8")
    assert content.startswith("---\ndescription:")
    assert "alwaysApply: false" in content
    assert "/v1/sandboxes/<SESSION_ID>/diagnostics/events" in content


def test_skills_install_refuses_to_overwrite_without_force(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / ".claude/skills/troubleshoot-sandbox/SKILL.md"
    destination.parent.mkdir(parents=True)
    destination.write_text("local instructions\n", encoding="utf-8")

    result = runner.invoke(app, ["skills", "install", "claude-code"])

    assert result.exit_code == 1
    assert "refusing to overwrite" in result.output
    assert destination.read_text(encoding="utf-8") == "local instructions\n"


def test_skills_install_force_replaces_existing_copy(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / ".cursor/rules/troubleshoot-sandbox.mdc"
    destination.parent.mkdir(parents=True)
    destination.write_text("stale\n", encoding="utf-8")

    result = runner.invoke(app, ["skills", "install", "cursor", "--force"])

    assert result.exit_code == 0, result.output
    assert destination.read_text(encoding="utf-8").startswith("---\ndescription:")


def test_skills_install_refuses_symlink_even_with_force(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    destination = tmp_path / ".claude/skills/troubleshoot-sandbox/SKILL.md"
    outside = tmp_path / "outside.txt"
    destination.parent.mkdir(parents=True)
    outside.write_text("keep me\n", encoding="utf-8")
    destination.symlink_to(outside)

    result = runner.invoke(app, ["skills", "install", "claude-code", "--force"])

    assert result.exit_code == 1
    assert "symlink" in result.output
    assert outside.read_text(encoding="utf-8") == "keep me\n"


def test_skills_install_help_lists_supported_targets() -> None:
    result = runner.invoke(app, ["skills", "install", "--help"])

    assert result.exit_code == 0, result.output
    assert "claude-code" in result.output
    assert "cursor" in result.output
