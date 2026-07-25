"""Wires all commands into the `boxkite` typer app. Individual commands live
in their own modules (cmd_*.py) to keep each file small and focused; this
module's only job is composition.
"""

from __future__ import annotations

import typer

from . import (
    cmd_allowlist,
    cmd_audit,
    cmd_config,
    cmd_exec,
    cmd_files,
    cmd_images,
    cmd_keys,
    cmd_log,
    cmd_mcp,
    cmd_secrets,
    cmd_session,
    cmd_signup,
    cmd_up,
    cmd_volumes,
    cmd_webhooks,
    cmd_whoami,
)
from .errors import cli_error_boundary

app = typer.Typer(
    name="boxkite",
    help=(
        "CLI for boxkite, a self-hostable sandbox for agent code execution. "
        "Use `boxkite up` for local docker-compose dev, or `boxkite signup` "
        "(or `boxkite config set-url`/`set-key`) to target a hosted control-plane."
    ),
    no_args_is_help=True,
)

config_app = typer.Typer(
    help="Manage hosted-mode configuration: the control-plane URL and your API key.",
    no_args_is_help=True,
)
config_app.command("set-key")(cli_error_boundary(cmd_config.set_key))
config_app.command("set-url")(cli_error_boundary(cmd_config.set_url))
config_app.command("show")(cli_error_boundary(cmd_config.show))
app.add_typer(config_app, name="config")

session_app = typer.Typer(
    help=(
        "Manage hosted sandbox sessions. Hosted mode only: local docker-compose mode "
        "(`boxkite up`) starts a single exec/file-ops sidecar with no session API of its own."
    ),
    no_args_is_help=True,
)
session_app.command("create")(cli_error_boundary(cmd_session.create))
session_app.command("ls")(cli_error_boundary(cmd_session.ls))
session_app.command("get")(cli_error_boundary(cmd_session.get))
session_app.command("rm")(cli_error_boundary(cmd_session.rm))
app.add_typer(session_app, name="session")

files_app = typer.Typer(
    help="View, create, edit, and search files in a sandbox (hosted session or local docker-compose sidecar).",
    no_args_is_help=True,
)
files_app.command("view")(cli_error_boundary(cmd_files.view))
files_app.command("create")(cli_error_boundary(cmd_files.create))
files_app.command("edit")(cli_error_boundary(cmd_files.edit))
files_app.command("ls")(cli_error_boundary(cmd_files.ls))
files_app.command("glob")(cli_error_boundary(cmd_files.glob))
files_app.command("grep")(cli_error_boundary(cmd_files.grep))
app.add_typer(files_app, name="files")

keys_app = typer.Typer(
    help="Manage API keys. Hosted mode only, and authenticated separately from "
    "sandbox operations -- these prompt for your account email/password each time.",
    no_args_is_help=True,
)
keys_app.command("ls")(cli_error_boundary(cmd_keys.ls))
keys_app.command("rm")(cli_error_boundary(cmd_keys.rm))
app.add_typer(keys_app, name="keys")

allowlist_app = typer.Typer(
    help=(
        "Manage the account's command allowlist for `boxkite exec` -- an opt-in "
        "guardrail against accidental/unexpected commands, not a sandbox-escape "
        "boundary (allowing an interpreter like python3/bash/node still permits "
        "arbitrary code). Hosted mode only. Empty allowlist means unrestricted."
    ),
    no_args_is_help=True,
)
allowlist_app.command("get")(cli_error_boundary(cmd_allowlist.get))
allowlist_app.command("set")(cli_error_boundary(cmd_allowlist.set_))
allowlist_app.command("clear")(cli_error_boundary(cmd_allowlist.clear))
app.add_typer(allowlist_app, name="allowlist")

secrets_app = typer.Typer(
    help=(
        "Manage org-scoped secrets for the sidecar's secrets-broker http_request tool. "
        "Hosted mode only. Values are write-only -- accepted on create, never shown again."
    ),
    no_args_is_help=True,
)
secrets_app.command("create")(cli_error_boundary(cmd_secrets.create))
secrets_app.command("ls")(cli_error_boundary(cmd_secrets.ls))
secrets_app.command("rm")(cli_error_boundary(cmd_secrets.rm))
app.add_typer(secrets_app, name="secrets")

images_app = typer.Typer(
    help=(
        "Build and manage declarative custom sandbox images (pre-approved base + "
        "exact-version-pinned packages). Hosted mode only, and only available on "
        "deployments with the declarative builder enabled server-side."
    ),
    no_args_is_help=True,
)
images_app.command("build")(cli_error_boundary(cmd_images.build))
images_app.command("get")(cli_error_boundary(cmd_images.get))
images_app.command("ls")(cli_error_boundary(cmd_images.ls))
images_app.command("rm")(cli_error_boundary(cmd_images.rm))
app.add_typer(images_app, name="images")

volumes_app = typer.Typer(
    help=(
        "Create and manage independent PVC-backed storage volumes, mountable into a "
        "sandbox session via `boxkite session create --volume-mounts`. Hosted mode only, "
        "and only available on deployments with independent volumes enabled server-side."
    ),
    no_args_is_help=True,
)
volumes_app.command("create")(cli_error_boundary(cmd_volumes.create))
volumes_app.command("get")(cli_error_boundary(cmd_volumes.get))
volumes_app.command("ls")(cli_error_boundary(cmd_volumes.ls))
volumes_app.command("rm")(cli_error_boundary(cmd_volumes.rm))
app.add_typer(volumes_app, name="volumes")

webhooks_app = typer.Typer(
    help=(
        "Manage registered webhooks for outbound sandbox event notifications "
        "(sandbox.created/sandbox.destroyed). Hosted mode only."
    ),
    no_args_is_help=True,
)
webhooks_app.command("create")(cli_error_boundary(cmd_webhooks.create))
webhooks_app.command("ls")(cli_error_boundary(cmd_webhooks.ls))
webhooks_app.command("rm")(cli_error_boundary(cmd_webhooks.rm))
webhooks_app.command("deliveries")(cli_error_boundary(cmd_webhooks.deliveries))
app.add_typer(webhooks_app, name="webhooks")

mcp_app = typer.Typer(
    help=(
        "Wire boxkite's MCP server into an MCP-compatible client's own config file "
        "(Claude Code, Cursor, Windsurf, Claude Desktop, Codex). Hosted mode only -- reads "
        "the base_url/api_key already saved by `boxkite signup` or `boxkite config`."
    ),
    no_args_is_help=True,
)
mcp_app.command("init")(cli_error_boundary(cmd_mcp.init))
app.add_typer(mcp_app, name="mcp")

audit_app = typer.Typer(
    help=(
        "Verify a HashChainedSQLiteAuditSink database file's hash chain for "
        "tamper-evidence (docs/TAMPER-EVIDENT-AUDIT-DESIGN.md). Local-only -- "
        "operates directly on a SQLite file, never a hosted control-plane."
    ),
    no_args_is_help=True,
)
audit_app.command("verify")(cli_error_boundary(cmd_audit.verify))
app.add_typer(audit_app, name="audit")

app.command("up")(cli_error_boundary(cmd_up.up))
app.command("exec")(cli_error_boundary(cmd_exec.exec_cmd))
app.command("signup")(cli_error_boundary(cmd_signup.signup))
app.command("whoami")(cli_error_boundary(cmd_whoami.whoami))
app.command("log")(cli_error_boundary(cmd_log.log))
app.command("watch")(cli_error_boundary(cmd_log.watch))


def main() -> None:
    app()


if __name__ == "__main__":
    main()
