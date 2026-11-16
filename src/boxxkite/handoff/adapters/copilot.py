"""GitHub Copilot CLI handoff adapter.

Verified on 2026-11-16 against `copilot --version` 1.0.112 (npm
`@github/copilot`, macOS arm64) with an isolated `COPILOT_HOME`:

- Session ids are lowercase UUIDs. `events.jsonl` holds the full event log;
  the sibling `workspace.yaml` and checkpoint/snapshot subdirectories are not
  needed (below).
- Copying only `events.jsonl` into a fresh `COPILOT_HOME` and running
  `copilot --resume <id>` from a different directory resumed the full
  conversation. Copilot regenerated `workspace.yaml` with the launch
  directory as its cwd, and resume still worked with every recorded cwd
  pointing at a nonexistent path, so unlike Qwen no cwd rewrite is needed.
- Headless auth reads `COPILOT_GITHUB_TOKEN`, then `GH_TOKEN`, then
  `GITHUB_TOKEN` (`copilot help environment`). `GH_TOKEN`/`GITHUB_TOKEN` are
  usually broad gh tokens, not a Copilot-scoped credential, so they are
  skipped.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from ..core import (
    Credential,
    HandoffError,
    LocatedSession,
    SessionFile,
    most_recent_by_mtime,
    validate_identifier,
)

SANDBOX_HOME = "/workspace"
TOKEN_ENV_VAR = "COPILOT_GITHUB_TOKEN"


class CopilotAdapter:
    """Locate a resumable GitHub Copilot CLI session and its scoped token."""

    name = "copilot"

    def __init__(self, *, copilot_home: Path | None = None) -> None:
        self._home = copilot_home or _default_home()

    def locate_session(self, *, session_ref: str | None = None) -> LocatedSession:
        events_path = _find_events_file(self._home, session_ref)
        session_id = validate_identifier(events_path.parent.name, what="session id")
        sandbox_path = f"{SANDBOX_HOME}/.copilot/session-state/{session_id}/events.jsonl"
        return LocatedSession(
            tool=self.name,
            session_id=session_id,
            files=(SessionFile(local_path=events_path, sandbox_path=sandbox_path),),
            credential=_resolve_credential(),
            resume_command=f"copilot --resume {session_id}",
            workdir=SANDBOX_HOME,
        )


def _default_home() -> Path:
    override = os.environ.get("COPILOT_HOME", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".copilot"


def _is_canonical_uuid(name: str) -> bool:
    try:
        return str(uuid.UUID(name)) == name
    except ValueError:
        return False


def _find_events_file(home: Path, session_ref: str | None) -> Path:
    state_dir = home / "session-state"
    candidates = [
        path
        for path in state_dir.glob("*/events.jsonl")
        if path.is_file() and _is_canonical_uuid(path.parent.name)
    ]
    if not candidates:
        raise HandoffError(
            f"No resumable GitHub Copilot CLI session found under {state_dir}. "
            "Run copilot locally before handing off a session."
        )
    if session_ref is None:
        return most_recent_by_mtime(candidates)
    try:
        wanted = str(uuid.UUID(session_ref))
    except ValueError as exc:
        raise HandoffError(f"Copilot session id {session_ref!r} is not a valid UUID.") from exc
    events_path = state_dir / wanted / "events.jsonl"
    if not events_path.is_file():
        raise HandoffError(f"No local GitHub Copilot CLI session found for id {wanted}.")
    return events_path


def _resolve_credential() -> Credential:
    token = os.environ.get(TOKEN_ENV_VAR, "").strip()
    if not token:
        raise HandoffError(
            f"{TOKEN_ENV_VAR} is not set: create a fine-grained personal access token "
            f"with the \"Copilot Requests\" permission and export it as {TOKEN_ENV_VAR}."
        )
    if token.startswith("ghp_"):
        raise HandoffError(
            f"{TOKEN_ENV_VAR} is a classic personal access token, which Copilot CLI does "
            "not support: use a fine-grained token with the \"Copilot Requests\" permission."
        )
    return Credential(env_var=TOKEN_ENV_VAR, value=token)
