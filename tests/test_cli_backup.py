"""CLI tests for `boxxkite backup`."""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

# handoff.terminal imports termios/tty (POSIX-only). Stub them so the shared
# CLI app still imports on Windows when this module is collected.
sys.modules.setdefault("termios", MagicMock())
sys.modules.setdefault("tty", MagicMock())

from typer.testing import CliRunner

from boxxkite.cli import app

runner = CliRunner()


def test_backup_create_inspect_restore_cli(tmp_path: Path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "note.txt").write_text("payload", encoding="utf-8")
    archive = tmp_path / "out.tar.gz"

    created = runner.invoke(
        app,
        [
            "backup",
            "create",
            "--output",
            str(archive),
            "--volume",
            f"uploads={src}",
            "--secrets-kms-backend",
            "local",
        ],
    )
    assert created.exit_code == 0, created.output
    assert "Backup written to" in created.output
    assert archive.is_file()

    inspected = runner.invoke(app, ["backup", "inspect", "--archive", str(archive)])
    assert inspected.exit_code == 0, inspected.output
    assert "has_postgres=False" in inspected.output
    assert "volumes=uploads" in inspected.output

    dest = tmp_path / "dest"
    restored = runner.invoke(
        app,
        [
            "backup",
            "restore",
            "--archive",
            str(archive),
            "--volume",
            f"uploads={dest}",
            "--yes",
        ],
    )
    assert restored.exit_code == 0, restored.output
    assert (dest / "note.txt").read_text(encoding="utf-8") == "payload"


def test_backup_create_requires_targets(tmp_path: Path):
    result = runner.invoke(app, ["backup", "create", "--output", str(tmp_path / "x.tar.gz")])
    assert result.exit_code == 1
    assert "Nothing to back up" in result.output


def test_backup_restore_postgres_requires_confirm(tmp_path: Path):
    dump = tmp_path / "dump.sql"
    dump.write_text("SELECT 1;\n", encoding="utf-8")
    archive = tmp_path / "pg.tar.gz"
    created = runner.invoke(
        app,
        ["backup", "create", "--output", str(archive), "--postgres-dump", str(dump)],
    )
    assert created.exit_code == 0, created.output

    result = runner.invoke(
        app,
        [
            "backup",
            "restore",
            "--archive",
            str(archive),
            "--database-url",
            "postgresql://localhost/boxxkite",
            "--yes",
        ],
    )
    assert result.exit_code == 1
    assert "confirm-key-available" in result.output
