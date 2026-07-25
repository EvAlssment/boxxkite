"""`boxkite files view/create/edit/ls/glob/grep` — file operations against a
sandbox, mirroring the control-plane's `/v1/sandboxes/{id}/files*` routes
(hosted) or the sidecar's own `/view`, `/file-create`, `/str-replace`, `/ls`,
`/glob`, `/grep` routes (local).
"""

from __future__ import annotations

from pathlib import Path

import typer

from .client import hosted_request, local_request, resolve_session_id
from .context import resolve_context
from .errors import CliError

_SESSION_HELP = "Hosted mode only: session ID to operate on. Auto-detected if exactly one active session exists."


def view(
    path: str = typer.Argument(..., help="File or directory path in the sandbox."),
    session: str | None = typer.Option(None, "--session", help=_SESSION_HELP),
    start_line: int | None = typer.Option(None, "--start-line", help="1-indexed start of an optional line range."),
    end_line: int | None = typer.Option(None, "--end-line", help="1-indexed end of an optional line range."),
) -> None:
    """View a file's contents, or list a directory, in the sandbox."""
    ctx = resolve_context()
    view_range = [start_line, end_line] if start_line is not None and end_line is not None else None
    body = {"path": path, "view_range": view_range}

    if ctx.mode == "hosted":
        session_id = resolve_session_id(ctx, session)
        result = hosted_request(ctx, "POST", f"/v1/sandboxes/{session_id}/files/view", json=body)
    else:
        result = local_request(ctx, "POST", "/view", json=body)

    if result.get("is_directory"):
        for entry in result.get("entries") or []:
            typer.echo(entry)
    else:
        typer.echo(result.get("content", ""))


def create(
    path: str = typer.Argument(..., help="Destination path in the sandbox."),
    content: str | None = typer.Option(None, "--content", help="File content, given inline."),
    content_file: Path | None = typer.Option(
        None, "--content-file", exists=True, readable=True, help="Read file content from this local path instead of --content."
    ),
    session: str | None = typer.Option(None, "--session", help=_SESSION_HELP),
) -> None:
    """Create or overwrite a file in the sandbox."""
    if content is None and content_file is None:
        raise CliError("Pass --content <text> or --content-file <local path>.")
    if content is not None and content_file is not None:
        raise CliError("Pass only one of --content or --content-file, not both.")

    body_content = content if content is not None else content_file.read_text()
    ctx = resolve_context()
    body = {"path": path, "content": body_content}

    if ctx.mode == "hosted":
        session_id = resolve_session_id(ctx, session)
        result = hosted_request(ctx, "POST", f"/v1/sandboxes/{session_id}/files", json=body)
    else:
        result = local_request(ctx, "POST", "/file-create", json=body)

    typer.echo(f"Wrote {result['path']} ({result['size']} bytes, created={result['created']})")


def ls(
    path: str = typer.Argument("/", help="Directory path in the sandbox."),
    session: str | None = typer.Option(None, "--session", help=_SESSION_HELP),
) -> None:
    """List a directory's direct children in the sandbox."""
    ctx = resolve_context()
    body = {"path": path}

    if ctx.mode == "hosted":
        session_id = resolve_session_id(ctx, session)
        result = hosted_request(ctx, "POST", f"/v1/sandboxes/{session_id}/files/ls", json=body)
    else:
        result = local_request(ctx, "POST", "/ls", json=body)

    for entry in result.get("entries") or []:
        suffix = "/" if entry.get("is_dir") else ""
        typer.echo(f"{entry['path']}{suffix}")


def glob(
    pattern: str = typer.Argument(..., help='Glob pattern, e.g. "**/*.py".'),
    path: str = typer.Option("/", "--path", help="Directory to search under."),
    session: str | None = typer.Option(None, "--session", help=_SESSION_HELP),
) -> None:
    """Find files in the sandbox by name pattern."""
    ctx = resolve_context()
    body = {"pattern": pattern, "path": path}

    if ctx.mode == "hosted":
        session_id = resolve_session_id(ctx, session)
        result = hosted_request(ctx, "POST", f"/v1/sandboxes/{session_id}/files/glob", json=body)
    else:
        result = local_request(ctx, "POST", "/glob", json=body)

    for match in result.get("matches") or []:
        suffix = "/" if match.get("is_dir") else ""
        typer.echo(f"{match['path']}{suffix}")


def grep(
    pattern: str = typer.Argument(..., help="Regex pattern to search file contents for."),
    path: str = typer.Option("/", "--path", help="Directory to search under."),
    glob_filter: str | None = typer.Option(None, "--glob", help='Restrict to files matching a glob, e.g. "*.py".'),
    max_matches: int = typer.Option(500, "--max-matches", help="Stop after this many matches."),
    session: str | None = typer.Option(None, "--session", help=_SESSION_HELP),
) -> None:
    """Search file contents in the sandbox by regex."""
    ctx = resolve_context()
    body = {"pattern": pattern, "path": path, "glob": glob_filter, "max_matches": max_matches}

    if ctx.mode == "hosted":
        session_id = resolve_session_id(ctx, session)
        result = hosted_request(ctx, "POST", f"/v1/sandboxes/{session_id}/files/grep", json=body)
    else:
        result = local_request(ctx, "POST", "/grep", json=body)

    for match in result.get("matches") or []:
        typer.echo(f"{match['path']}:{match['line']}:{match['text']}")
    if result.get("truncated"):
        typer.echo(f"(truncated at {max_matches} matches)")


def edit(
    path: str = typer.Argument(..., help="File path in the sandbox."),
    old_str: str = typer.Option(..., "--old", help="Exact string to find; must appear exactly once unless --replace-all."),
    new_str: str = typer.Option(..., "--new", help="Replacement string."),
    replace_all: bool = typer.Option(False, "--replace-all", help="Replace every occurrence instead of requiring exactly one."),
    session: str | None = typer.Option(None, "--session", help=_SESSION_HELP),
) -> None:
    """Replace a string in a sandbox file (str-replace)."""
    ctx = resolve_context()
    body = {"path": path, "old_str": old_str, "new_str": new_str, "replace_all": replace_all}

    if ctx.mode == "hosted":
        session_id = resolve_session_id(ctx, session)
        result = hosted_request(ctx, "POST", f"/v1/sandboxes/{session_id}/files/str-replace", json=body)
    else:
        result = local_request(ctx, "POST", "/str-replace", json=body)

    if result["replaced"]:
        typer.echo(f"Replaced {result['occurrences']} occurrence(s) in {result['path']}")
    else:
        typer.echo(f"No replacement made in {result['path']} (occurrences={result['occurrences']})")
