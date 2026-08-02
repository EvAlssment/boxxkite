"""Codex CLI (openai/codex) handoff adapter.

Session-file layout and resume mechanism were verified directly against
openai/codex's own source (2026-07), not assumed from CLI docs:

- Rollout files live at
  ``$CODEX_HOME/sessions/YYYY/MM/DD/rollout-<timestamp>-<uuid>.jsonl``
  (see ``codex-rs/rollout/src/recorder.rs``'s ``precompute_log_file_info``).
  ``CODEX_HOME`` defaults to ``~/.codex``
  (``codex-rs/utils/home-dir/src/lib.rs``).
- The previously-assumed ``-c experimental_resume=<path>`` flag does **not**
  exist in current openai/codex -- it has been removed. The real, current
  mechanism is ``codex resume <thread-uuid>`` / ``codex resume --last``,
  which looks the id up inside ``$CODEX_HOME/sessions`` itself
  (``find_thread_path_by_id_str`` in ``codex-rs/rollout/src/list.rs``, with a
  state-db fast path and a filesystem-scan fallback). This is still *not*
  cwd-sensitive the way Claude Code's resume is -- there is no cwd encoded
  anywhere in the lookup -- so this adapter's ``workdir`` is just
  ``/workspace``, but it is also not a literal "arbitrary path" flag: the
  pushed rollout file has to land at the same relative path under the
  sandbox's ``$CODEX_HOME/sessions/`` tree, which is exactly what
  ``sandbox_path`` below reproduces.
- Credentials: ``OPENAI_API_KEY`` and ``CODEX_API_KEY`` env vars are read as
  plain API keys (``codex-rs/login/src/auth/manager.rs``); a personal access
  token (prefixed ``at-``) is read from ``CODEX_ACCESS_TOKEN``. All three,
  plus a persisted ``$CODEX_HOME/auth.json``, are handled below. A
  ChatGPT-plan login with *only* a raw OAuth ``tokens`` entry in
  ``auth.json`` (no API key, no personal access token) is deliberately
  **not** treated as portable -- see ``_resolve_credential``'s docstring.

Known limitation: Codex's background rollout-compression worker can rewrite
cold rollout files to ``.jsonl.zst`` (``codex-rs/rollout/src/compression.rs``).
This adapter only reads plain ``.jsonl`` rollout files -- an already-compressed
session (typically an older, inactive one) will not be found. This should
cover the sessions someone would actually want to hand off (a still-active
local conversation), but it is not a complete replay of every historical
session on disk.
"""

from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from dataclasses import dataclass
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
CODEX_HOME_DIR_NAME = ".codex"
SESSIONS_SUBDIR = "sessions"

_ROLLOUT_FILENAME_RE = re.compile(
    r"^rollout-\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2}-"
    r"(?P<uuid>[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12})"
    r"\.jsonl$"
)


@dataclass(frozen=True)
class _RolloutFile:
    path: Path
    session_id: str
    year: str
    month: str
    day: str


class CodexAdapter:
    """Locates a local Codex CLI rollout session and a usable credential."""

    name = "codex"

    def __init__(self, *, codex_home: Path | None = None) -> None:
        self._codex_home = codex_home if codex_home is not None else _default_codex_home()

    def locate_session(self, *, session_ref: str | None = None) -> LocatedSession:
        rollout = _find_rollout_file(self._codex_home, session_ref)
        session_id = validate_identifier(rollout.session_id, what="session id")
        credential = _resolve_credential(self._codex_home)

        sandbox_path = "/".join(
            (
                SANDBOX_HOME,
                CODEX_HOME_DIR_NAME,
                SESSIONS_SUBDIR,
                rollout.year,
                rollout.month,
                rollout.day,
                rollout.path.name,
            )
        )
        return LocatedSession(
            tool=self.name,
            session_id=session_id,
            files=(SessionFile(local_path=rollout.path, sandbox_path=sandbox_path),),
            credential=credential,
            resume_command=f"codex resume {session_id}",
            workdir=SANDBOX_HOME,
        )


def _default_codex_home() -> Path:
    override = os.environ.get("CODEX_HOME", "").strip()
    if override:
        return Path(override)
    return Path.home() / CODEX_HOME_DIR_NAME


def _find_rollout_file(codex_home: Path, session_ref: str | None) -> _RolloutFile:
    sessions_root = codex_home / SESSIONS_SUBDIR
    if not sessions_root.is_dir():
        raise HandoffError(
            f"No Codex sessions directory found at {sessions_root}. Run Codex CLI at "
            "least once locally (so it has recorded a rollout) before handing off a "
            "session."
        )

    candidates = list(_iter_rollout_files(sessions_root))
    if not candidates:
        raise HandoffError(
            f"No local Codex rollout session files (rollout-*.jsonl) found under "
            f"{sessions_root}. If Codex has been idle for a while, its background "
            "compression worker may have rewritten recent rollouts to .jsonl.zst, "
            "which this adapter does not currently read -- start a new turn in "
            "Codex to produce a fresh, uncompressed rollout, then retry."
        )

    if session_ref is not None:
        matches = [c for c in candidates if c.session_id == session_ref]
        if not matches:
            raise HandoffError(
                f"No local Codex rollout file found for session id {session_ref!r} "
                f"under {sessions_root}."
            )
        return matches[0]

    most_recent_path = most_recent_by_mtime(c.path for c in candidates)
    return next(c for c in candidates if c.path == most_recent_path)


def _iter_rollout_files(sessions_root: Path) -> Iterator[_RolloutFile]:
    for path in sessions_root.rglob("rollout-*.jsonl"):
        if not path.is_file():
            continue
        match = _ROLLOUT_FILENAME_RE.match(path.name)
        if match is None:
            continue
        rel_parts = path.relative_to(sessions_root).parts
        if len(rel_parts) < 4:
            continue
        year, month, day = rel_parts[0], rel_parts[1], rel_parts[2]
        yield _RolloutFile(path=path, session_id=match.group("uuid"), year=year, month=month, day=day)


def _resolve_credential(codex_home: Path) -> Credential:
    """Resolve the portable credential Codex should authenticate with.

    Priority: explicit env vars first (matching Codex's own precedence of
    ``CODEX_API_KEY``/``CODEX_ACCESS_TOKEN`` over persisted auth, plus the
    conventional ``OPENAI_API_KEY``), then ``$CODEX_HOME/auth.json``.

    ``auth.json`` can hold three different things (see
    ``codex-rs/login/src/auth/storage.rs``'s ``AuthDotJson``):

    - ``personal_access_token`` -- a scoped, independently-revocable token
      (prefixed ``at-``, verified server-side via a ``whoami`` call). This is
      genuinely portable and is what this adapter prefers when no env var is
      set; it maps to ``CODEX_ACCESS_TOKEN``, the env var Codex itself reads
      for it.
    - ``OPENAI_API_KEY`` -- a plain API key, equally portable.
    - ``tokens`` -- the raw ChatGPT-plan OAuth access/refresh token pair for
      *this machine's* logged-in session. This is deliberately **not**
      treated as a usable portable credential: it isn't independently scoped
      or easily revocable the way the two options above are, and copying it
      elsewhere hands over the same live session used locally. If this is
      all that's on file, this function raises ``HandoffError`` telling the
      user to set up an API key or personal access token instead of silently
      shipping the raw OAuth tokens.
    """
    for env_var in ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN"):
        value = os.environ.get(env_var, "").strip()
        if value:
            return Credential(env_var=env_var, value=value)

    auth_path = codex_home / "auth.json"
    if auth_path.is_file():
        try:
            auth_data = json.loads(auth_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise HandoffError(f"Found {auth_path} but could not read it: {exc}") from exc

        personal_access_token = auth_data.get("personal_access_token")
        if isinstance(personal_access_token, str) and personal_access_token.strip():
            return Credential(env_var="CODEX_ACCESS_TOKEN", value=personal_access_token.strip())

        api_key = auth_data.get("OPENAI_API_KEY")
        if isinstance(api_key, str) and api_key.strip():
            return Credential(env_var="OPENAI_API_KEY", value=api_key.strip())

        if auth_data.get("tokens"):
            raise HandoffError(
                f"{auth_path} only has a ChatGPT-plan OAuth session on file (its "
                "'tokens' field) -- that access/refresh token pair is this machine's "
                "own live session, not a portable, independently-revocable "
                "credential, so this adapter won't copy it into a sandbox. Run "
                "`codex login --api-key <key>` or generate a personal access token, "
                "or set OPENAI_API_KEY / CODEX_API_KEY / CODEX_ACCESS_TOKEN in the "
                "environment, then retry."
            )

    raise HandoffError(
        "No usable Codex credential found: set OPENAI_API_KEY (or CODEX_API_KEY / "
        f"CODEX_ACCESS_TOKEN) in the environment, or log in with `codex login` so "
        f"{auth_path} contains an API key or personal access token."
    )
