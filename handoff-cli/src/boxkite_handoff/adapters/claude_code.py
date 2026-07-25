"""Claude Code adapter -- see docs/handoff-adapters.md for the full contract.

Session storage (verified directly against the installed `claude` CLI on
this machine, not assumed from docs alone): sessions are JSONL files at
`$CLAUDE_CONFIG_DIR/projects/<encoded-cwd>/<session-id>.jsonl` (defaulting to
`~/.claude` when `CLAUDE_CONFIG_DIR` is unset). `<encoded-cwd>` is the
session's absolute working directory with every `/` and `.` replaced by `-`
-- confirmed empirically by running `claude` with `CLAUDE_CONFIG_DIR` pointed
at a scratch directory from several real cwds (including one with a hidden,
dotted path segment) and inspecting the resulting `projects/` subdirectory
name each time; see `encode_project_dir` below.

Resume is cwd-sensitive for *both* `--continue` (documented) and the
explicit `--resume <session-id>` form (not obviously documented, but
independently confirmed here: running `claude --resume <real-session-id>
--print ...` from a directory other than the session's original cwd fails
immediately, before any model call, with "No conversation found with
session ID: ..." -- the lookup is keyed on the encoded *current* cwd, not
just the id).

Working-directory mapping -- the one deliberate simplification in this
adapter, spelled out here rather than left implicit: instead of trying to
reproduce the original local absolute path (which is arbitrary, may live
outside `/workspace`, and would require replicating the entire project's
file tree -- source files, `.git`, etc. -- for the agent to have real file
access matching what the transcript references), this adapter always
resumes from a fixed sandbox cwd, `/workspace` (the sandbox's `$HOME`,
guaranteed to already exist). The session JSONL is pushed to
`/workspace/.claude/projects/<encode("/workspace")>/<session-id>.jsonl`,
and `workdir` is `/workspace` -- so the encoded-cwd lookup lines up without
depending on any nested `/workspace/<...>` directory being created first.

**What this does and does not achieve**: the conversation transcript itself
resumes in full -- every prior message, tool call, and tool result Claude
Code recorded is there, satisfying "full conversation continuity," not a
diff/task summary. What it does *not* do is copy the original project's
source files into the sandbox -- if the resumed conversation references
repo files, they will not be present at `/workspace` unless something else
(the caller, or a future version of this adapter) puts them there.
Replicating the underlying working tree (respecting `.gitignore`, size
limits, and avoiding secrets) is a real, nontrivial feature left as a
documented follow-up, not attempted here.

Credential: `claude setup-token` (run locally, ahead of time, by the user)
mints a long-lived, scoped token for headless use -- confirmed directly
against the installed CLI (`claude setup-token --help` describes it as
setting up "a long-lived authentication token"). This adapter reads it from
the `CLAUDE_CODE_OAUTH_TOKEN` environment variable only, per
docs/handoff-adapters.md's credential table; it never reads the raw local
OAuth session (on this machine that's a macOS Keychain entry named "Claude
Code-credentials", confirmed present via `security find-generic-password`
-- an intentionally different, non-portable credential this adapter must
never fall back to).
"""

from __future__ import annotations

import os
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
ORPHANED_MARKER = ".orphaned-"
OAUTH_TOKEN_ENV_VAR = "CLAUDE_CODE_OAUTH_TOKEN"
CONFIG_DIR_ENV_VAR = "CLAUDE_CONFIG_DIR"


def encode_project_dir(absolute_path: str) -> str:
    """Claude Code's own cwd -> project-directory-name encoding: every `/`
    and `.` becomes `-`. See module docstring for how this was verified."""
    return absolute_path.replace("/", "-").replace(".", "-")


class ClaudeCodeAdapter:
    """`config_dir`/`cwd` are overridable for tests; left as `None` in real
    use so resolution happens lazily, against the real environment, at
    `locate_session()` time rather than at process-argv-parsing time."""

    name = "claude-code"

    def __init__(self, *, config_dir: Path | None = None, cwd: Path | None = None) -> None:
        self._config_dir = config_dir
        self._cwd = cwd

    def locate_session(self, *, session_ref: str | None = None) -> LocatedSession:
        config_dir = self._config_dir or _default_config_dir()
        cwd = self._cwd or Path.cwd()

        project_dir = config_dir / "projects" / encode_project_dir(str(cwd))
        session_id, session_path = _find_session(project_dir, cwd, session_ref)
        token = _find_oauth_token()

        sandbox_project_dir = encode_project_dir(SANDBOX_HOME)
        sandbox_session_path = f"{SANDBOX_HOME}/.claude/projects/{sandbox_project_dir}/{session_id}.jsonl"

        return LocatedSession(
            tool=self.name,
            session_id=session_id,
            files=(SessionFile(local_path=session_path, sandbox_path=sandbox_session_path),),
            credential=Credential(env_var=OAUTH_TOKEN_ENV_VAR, value=token),
            resume_command=f"claude --resume {session_id}",
            workdir=SANDBOX_HOME,
        )


def _default_config_dir() -> Path:
    override = os.environ.get(CONFIG_DIR_ENV_VAR)
    return Path(override).expanduser() if override else Path.home() / ".claude"


def _find_session(project_dir: Path, cwd: Path, session_ref: str | None) -> tuple[str, Path]:
    if session_ref is not None:
        session_ref = validate_identifier(session_ref, what="session id")
        candidate = project_dir / f"{session_ref}.jsonl"
        if not candidate.is_file():
            raise HandoffError(f"No Claude Code session {session_ref!r} found in {project_dir}")
        return session_ref, candidate

    if not project_dir.is_dir():
        raise HandoffError(
            f"No Claude Code session directory found for {cwd} (expected {project_dir}). "
            "Run `claude` from this directory at least once before handing off."
        )

    candidates = [p for p in project_dir.glob("*.jsonl") if ORPHANED_MARKER not in p.name]
    if not candidates:
        raise HandoffError(f"No local Claude Code session found for {cwd} in {project_dir}")

    most_recent = most_recent_by_mtime(candidates)
    session_id = validate_identifier(most_recent.stem, what="session id (from filename)")
    return session_id, most_recent


def _find_oauth_token() -> str:
    token = os.environ.get(OAUTH_TOKEN_ENV_VAR)
    if not token:
        raise HandoffError(
            f"{OAUTH_TOKEN_ENV_VAR} is not set. Run `claude setup-token` locally once to mint a "
            f"long-lived, scoped token, then export {OAUTH_TOKEN_ENV_VAR} before running "
            "boxkite-handoff (see docs/handoff-adapters.md's credential table)."
        )
    return token
