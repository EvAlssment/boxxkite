"""Helm chart upgrade compatibility checks."""

from __future__ import annotations

import json
import re
from pathlib import Path

import typer

DEFAULT_MATRIX_PATH = Path("deploy/helm/boxxkite/COMPATIBILITY.json")
_VERSION_RE = re.compile(r"v?(\d+)\.(\d+)\.(\d+)")


def _version_key(version: str) -> tuple[int, int, int]:
    match = _VERSION_RE.fullmatch(version.strip())
    if not match:
        raise typer.BadParameter(f"invalid semantic version: {version!r}")
    return tuple(int(part) for part in match.groups())


def _load_matrix(path: Path) -> list[dict]:
    try:
        payload = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise typer.BadParameter(f"compatibility matrix not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise typer.BadParameter(f"invalid compatibility matrix {path}: {exc}") from exc

    entries = payload.get("versions")
    if not isinstance(entries, list) or not entries:
        raise typer.BadParameter(f"compatibility matrix has no versions: {path}")
    if any(not isinstance(entry, dict) or "version" not in entry for entry in entries):
        raise typer.BadParameter("every compatibility entry must contain a version")
    return sorted(entries, key=lambda entry: _version_key(entry["version"]))


def check(
    from_version: str = typer.Option(..., "--from", help="Installed chart version."),
    to_version: str = typer.Option(..., "--to", help="Target chart version."),
    matrix: Path = typer.Option(DEFAULT_MATRIX_PATH, "--matrix", help="Compatibility matrix JSON."),
    allow_breaking: bool = typer.Option(
        False,
        "--allow-breaking",
        help="Acknowledge breaking entries after reviewing their migration steps.",
    ),
) -> None:
    """Print the ordered chart migration path and stop on unacknowledged breaks."""
    from_key = _version_key(from_version)
    to_key = _version_key(to_version)
    if from_key >= to_key:
        raise typer.BadParameter("--to must be newer than --from")

    entries = _load_matrix(matrix)
    by_version = {_version_key(entry["version"]): entry for entry in entries}
    if from_key not in by_version:
        raise typer.BadParameter(f"--from version is absent from {matrix}")
    if to_key not in by_version:
        raise typer.BadParameter(f"--to version is absent from {matrix}")

    path = [by_version[key] for key in sorted(by_version) if from_key < key <= to_key]
    typer.echo(f"Migration path: {from_version} -> {to_version}")
    for entry in path:
        marker = "BREAKING" if entry.get("breaking") else "compatible"
        typer.echo(f"- {entry['version']} [{marker}]: {entry.get('migration', 'No migration note.')}")

    breaking = [entry for entry in path if entry.get("breaking")]
    if breaking and not allow_breaking:
        typer.echo(
            "\nUpgrade stopped: review the breaking migration entries, then rerun with --allow-breaking.",
            err=True,
        )
        raise typer.Exit(code=2)
