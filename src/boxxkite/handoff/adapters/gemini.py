"""Gemini CLI handoff adapter.

Locates local Gemini CLI sessions and prepares them for resumption in a boxxkite
sandbox.

Gemini CLI stores session logs under ``~/.gemini/tmp/` (or ``$GEMINI_HOME/tmp/``)
as JSON / JSONL files.
Credentials are read from the ``GEMINI_API_KEY`` environment variable or from
``$GEMINI_HOME/settings.json``.
"""

from __future__ import annotations

import json
import os
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
GEMINI_HOME_DIR_NAME = ".gemini"
SESSIONS_SUBDIR = "tmp"


@dataclass(frozen=True)
class _GeminiSessionFile:
    path: Path
    session_id: str


class GeminiAdapter:
    """Locates a local Gemini CLI session and a usable credential."""

    name = "gemini"

    def __init__(self, *, gemini_home: Path | None = None) -> None:
        self._gemini_home = gemini_home if gemini_home is not None else _default_gemini_home()

    def locate_session(self, *, session_ref: str | None = None) -> LocatedSession:
        session_file = _find_session_file(self._gemini_home, session_ref)
        session_id = validate_identifier(session_file.session_id, what="session id")
        credential = _resolve_credential(self._gemini_home)

        sandbox_path = "/".join(
            (
                SANDBOX_HOME,
                GEMINI_HOME_DIR_NAME,
                SESSIONS_SUBDIR,
                session_file.path.name,
            )
        )
        return LocatedSession(
            tool=self.name,
            session_id=session_id,
            files=(SessionFile(local_path=session_file.path, sandbox_path=sandbox_path),),
            credential=credential,
            resume_command=f"gemini --resume {session_id}",
            workdir=SANDBOX_HOME,
        )


def _default_gemini_home() -> Path:
    override = os.environ.get("GEMINI_HOME", "").strip()
    if override:
        return Path(override)
    return Path.home() / GEMINI_HOME_DIR_NAME


def _find_session_file(gemini_home: Path, session_ref: str | None) -> _GeminiSessionFile:
    sessions_root = gemini_home / SESSIONS_SUBDIR
    if not sessions_root.is_dir():
        raise HandoffError(
            f"No Gemini sessions directory found at {sessions_root}. Run Gemini CLI at "
            "least once locally before handing off a session."
        )

    candidates = [
        _GeminiSessionFile(path=p, session_id=p.stem)
        for p in sessions_root.glob("*.json")
        if p.is_file()
    ]
    if not candidates:
        raise HandoffError(f"No local Gemini session files (*.json) found under {sessions_root}.")

    if session_ref is not None:
        matches = [c for c in candidates if c.session_id == session_ref]
        if not matches:
            raise HandoffError(
                f"No local Gemini session file found for session id {session_ref!r} "
                f"under {sessions_root}."
            )
        return matches[0]

    most_recent_path = most_recent_by_mtime(c.path for c in candidates)
    return next(c for c in candidates if c.path == most_recent_path)


def _resolve_credential(gemini_home: Path) -> Credential:
    """Resolve the portable credential Gemini CLI should authenticate with."""
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        return Credential(env_var="GEMINI_API_KEY", value=api_key)

    settings_path = gemini_home / "settings.json"
    if settings_path.is_file():
        try:
            data = json.loads(settings_path.read_text(encoding="utf-8"))
            key = data.get("api_key") or data.get("GEMINI_API_KEY")
            if isinstance(key, str) and key.strip():
                return Credential(env_var="GEMINI_API_KEY", value=key.strip())
        except (OSError, json.JSONDecodeError) as exc:
            raise HandoffError(f"Found {settings_path} but could not read it: {exc}") from exc

    raise HandoffError(
        "No usable Gemini credential found: set GEMINI_API_KEY in your environment "
        f"or configure your API key in {settings_path}."
    )
