import json
from pathlib import Path

from typer.testing import CliRunner

from boxxkite.cli import app


runner = CliRunner()


def _matrix(tmp_path: Path, *entries: dict) -> Path:
    path = tmp_path / "COMPATIBILITY.json"
    path.write_text(json.dumps({"schema_version": 1, "chart": "boxxkite", "versions": entries}))
    return path


def test_upgrade_check_prints_ordered_path(tmp_path):
    matrix = _matrix(
        tmp_path,
        {"version": "1.0.0", "breaking": False, "migration": "initial"},
        {"version": "1.1.0", "breaking": False, "migration": "rename nothing"},
        {"version": "2.0.0", "breaking": False, "migration": "manual review"},
    )

    result = runner.invoke(
        app,
        ["upgrade", "check", "--from", "1.0.0", "--to", "2.0.0", "--matrix", str(matrix)],
    )

    assert result.exit_code == 0
    lines = result.output.splitlines()
    assert lines.index("- 1.1.0 [compatible]: rename nothing") < lines.index(
        "- 2.0.0 [compatible]: manual review"
    )


def test_upgrade_check_stops_on_breaking_entry_until_acknowledged(tmp_path):
    matrix = _matrix(
        tmp_path,
        {"version": "1.0.0", "breaking": False, "migration": "initial"},
        {"version": "2.0.0", "breaking": True, "migration": "apply RBAC update"},
    )

    blocked = runner.invoke(
        app,
        ["upgrade", "check", "--from", "1.0.0", "--to", "2.0.0", "--matrix", str(matrix)],
    )
    allowed = runner.invoke(
        app,
        [
            "upgrade",
            "check",
            "--from",
            "1.0.0",
            "--to",
            "2.0.0",
            "--matrix",
            str(matrix),
            "--allow-breaking",
        ],
    )

    assert blocked.exit_code == 2
    assert "Upgrade stopped" in blocked.output
    assert allowed.exit_code == 0
