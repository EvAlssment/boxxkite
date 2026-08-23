"""Focused tests for the local, non-networking `boxxkite new` command."""

from __future__ import annotations

from typer.testing import CliRunner

from boxxkite.cli import app

runner = CliRunner()


def test_new_creates_minimal_python_project(tmp_path):
    destination = tmp_path / "hello-agent"

    result = runner.invoke(
        app,
        [
            "new",
            str(destination),
            "--language",
            "python",
            "--framework",
            "plain",
            "--use-case",
            "code-interpreter",
        ],
    )

    assert result.exit_code == 0, result.output
    assert "Created boxxkite project" in result.output
    assert sorted(path.name for path in destination.iterdir()) == [
        ".env.example",
        ".gitignore",
        "README.md",
        "app.py",
        "requirements.txt",
    ]
    assert "BOXXKITE_API_KEY" in (destination / ".env.example").read_text()
    app_source = (destination / "app.py").read_text()
    readme = (destination / "README.md").read_text()
    compile(app_source, str(destination / "app.py"), "exec")
    assert "BOXXKITE_API_KEY" in app_source
    assert "__PROJECT_LABEL__" not in app_source
    assert "hello-agent" in readme
    assert "__PROJECT_NAME__" not in readme
    assert ".env" in (destination / ".gitignore").read_text()


def test_new_refuses_to_overwrite_existing_path(tmp_path):
    destination = tmp_path / "existing"
    destination.mkdir()
    marker = destination / "keep.txt"
    marker.write_text("do not touch")

    result = runner.invoke(app, ["new", str(destination)])

    assert result.exit_code == 1
    assert "Refusing to overwrite" in result.output
    assert marker.read_text() == "do not touch"
    assert list(destination.iterdir()) == [marker]


def test_new_validates_choices_before_creating_anything(tmp_path):
    destination = tmp_path / "invalid"

    result = runner.invoke(app, ["new", str(destination), "--framework", "langchain"])

    assert result.exit_code == 1
    assert "Unsupported --framework 'langchain'" in result.output
    assert not destination.exists()


def test_new_rejects_unsafe_project_directory_name(tmp_path):
    destination = tmp_path / "not a project"

    result = runner.invoke(app, ["new", str(destination)])

    assert result.exit_code == 1
    assert "Invalid project directory" in result.output
    assert not destination.exists()


def test_new_does_not_create_live_env_file(tmp_path):
    destination = tmp_path / "safe-start"

    result = runner.invoke(app, ["new", str(destination)])

    assert result.exit_code == 0, result.output
    assert not (destination / ".env").exists()
    assert "your-api-key-here" in (destination / ".env.example").read_text()
