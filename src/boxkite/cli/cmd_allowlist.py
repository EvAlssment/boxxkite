"""`boxkite allowlist get/set/clear` — hosted-mode per-account command
allowlist management.

This is an OPT-IN guardrail, not a sandbox-escape boundary: it restricts
which command names (and, optionally, which argument patterns) `boxkite
exec` is allowed to run, but if you allow an interpreter like `python3`,
`bash`, or `node`, that interpreter can still run arbitrary code. Treat it
as a tripwire for accidental/unexpected commands, not as isolation.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from .client import hosted_request
from .context import resolve_context
from .errors import CliError

ENDPOINT = "/v1/account/allowed-commands"


def _format_rule(rule: str | dict) -> str:
    if isinstance(rule, str):
        return rule
    if isinstance(rule, dict):
        command = rule.get("command", "?")
        args_allow = rule.get("args_allow") or []
        args_deny = rule.get("args_deny") or []
        return f"{command} (args_allow={args_allow!r}, args_deny={args_deny!r})"
    return str(rule)


def get() -> None:
    """Show the account's current command allowlist.

    An empty allowlist means unrestricted: `boxkite exec` can run any
    command. This is an opt-in guardrail, not a sandbox boundary --
    allowing an interpreter (python3, bash, node, ...) still permits
    arbitrary code inside it.
    """
    ctx = resolve_context()
    result = hosted_request(ctx, "GET", ENDPOINT)
    rules = result.get("rules", []) if isinstance(result, dict) else []
    if not rules:
        typer.echo("Unrestricted (no custom allowlist set)")
        return
    for rule in rules:
        typer.echo(_format_rule(rule))


def set_(
    path: Path = typer.Argument(
        ..., exists=True, dir_okay=False, readable=True, help="Path to a JSON file containing an array of rules."
    ),
) -> None:
    """Replace the account's command allowlist from a JSON file.

    The file must contain a JSON array where each entry is either a plain
    command-name string, or an object like
    {"command": "...", "args_allow": ["..."], "args_deny": ["..."]}
    (args_allow/args_deny are Python regexes matched against the command's
    arguments).

    Remember this is an opt-in guardrail against accidental commands, not
    a hard isolation boundary -- allowing an interpreter (python3, bash,
    node, ...) still permits arbitrary code once it's running.
    """
    ctx = resolve_context()
    try:
        raw = path.read_text()
    except OSError as exc:
        raise CliError(f"Could not read {path}: {exc}") from exc

    try:
        rules = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CliError(f"{path} is not valid JSON: {exc}") from exc

    if not isinstance(rules, list):
        raise CliError(f"{path} must contain a JSON array of rules, got {type(rules).__name__}.")

    result = hosted_request(ctx, "PUT", ENDPOINT, json={"rules": rules})
    saved = result.get("rules", []) if isinstance(result, dict) else []
    typer.echo(f"Saved allowlist with {len(saved)} rule(s).")


def clear() -> None:
    """Clear the account's command allowlist back to unrestricted."""
    ctx = resolve_context()
    hosted_request(ctx, "DELETE", ENDPOINT)
    typer.echo("Cleared allowlist (account is now unrestricted).")
