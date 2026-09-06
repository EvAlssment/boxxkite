"""Install the packaged sandbox troubleshooting instructions for coding agents."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

import typer

from .errors import CliError

SKILL_NAME = "troubleshoot-sandbox"
TARGETS = ("claude-code", "cursor")

_ASSETS = {
    "claude-code": (
        "SKILL.md",
        Path(".claude") / "skills" / SKILL_NAME / "SKILL.md",
    ),
    "cursor": (
        "troubleshoot-sandbox.mdc",
        Path(".cursor") / "rules" / "troubleshoot-sandbox.mdc",
    ),
}


def _asset_text(target: str) -> str:
    asset_name, _ = _ASSETS[target]
    return (
        files("boxxkite.skill_assets.troubleshoot_sandbox")
        .joinpath(asset_name)
        .read_text(encoding="utf-8")
    )


def _destination(target: str, root: Path) -> Path:
    return root / _ASSETS[target][1]


def install(
    target: str = typer.Argument(
        ..., help=f"Agent to configure. Choose one of: {', '.join(TARGETS)}."
    ),
    force: bool = typer.Option(False, "--force", help="Replace an existing copy of this skill."),
) -> None:
    """Install the sandbox troubleshooting skill in the current project."""
    if target not in TARGETS:
        raise CliError(f"Unknown target {target!r}. Choose one of: {', '.join(TARGETS)}.")

    destination = _destination(target, Path.cwd())
    if destination.is_symlink():
        raise CliError(
            f"{destination} is a symlink; refusing to overwrite it. "
            "Remove it before installing the skill."
        )
    if destination.exists() and not force:
        raise CliError(
            f"{destination} already exists; refusing to overwrite it. "
            "Pass --force to replace it with the packaged skill."
        )

    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(_asset_text(target), encoding="utf-8")
    typer.secho(f"Installed {SKILL_NAME} for {target} at {destination}.", fg=typer.colors.GREEN)
