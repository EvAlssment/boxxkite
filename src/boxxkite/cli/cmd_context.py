from __future__ import annotations

import typer

from ..handoff.context import CONTEXT_READERS, artifact_to_json, export_context, render_prompt
from ..handoff.core import HandoffError
from .errors import CliError

TOOL_NAMES = sorted(CONTEXT_READERS)


def export(
    tool: str = typer.Argument(..., help=f"Which local tool to read a session from: {', '.join(TOOL_NAMES)}."),
    session_ref: str | None = typer.Option(
        None,
        "--session",
        help=(
            "Session id to read; defaults to the most recent one "
            "(claude-code searches the current directory's project)."
        ),
    ),
    last: int | None = typer.Option(None, "--last", min=1, help="Keep only the newest N messages."),
    prompt: bool = typer.Option(
        False, "--prompt", help="Print a prompt for a fresh agent session instead of the JSON artifact."
    ),
    max_chars: int | None = typer.Option(
        None,
        "--max-chars",
        min=1,
        help="With --prompt, cap the prompt length by dropping the oldest messages (default 60000).",
    ),
) -> None:
    """Print a portable context artifact for one local session on stdout."""
    if max_chars is not None and not prompt:
        raise CliError("--max-chars only applies with --prompt.")

    try:
        artifact = export_context(tool, session_ref=session_ref, last_messages=last)
        if not prompt:
            output = artifact_to_json(artifact)
        elif max_chars is None:
            output = render_prompt(artifact)
        else:
            output = render_prompt(artifact, max_chars=max_chars)
    except HandoffError as exc:
        raise CliError(str(exc)) from exc
    typer.echo(output)
