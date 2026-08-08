"""Kimi CLI adapter handoff

Locates local Kimi Code CLI sessions and prepares them for resumption in a boxxkite
sandbox.

Kimi Code CLI stores session data under ``$KIMI_CODE_HOME/sessions/<session_id>/``
(``KIMI_CODE_HOME`` defaults to ``~/.kimi-code``, or fallback ``~/.kimi`` for older CLI versions).
Credentials are read from ``KIMI_API_KEY`` or ``MOONSHOT_API_KEY`` environment variables.
"""

from __future__ import annotations

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
KIMI_CODE_HOME_DIR_NAME = ".kimi-code"
KIMI_LEGACY_HOME_DIR_NAME = ".kimi"


@dataclass(frozen=True)
class _KimiSessionFile:
    path: Path
    session_id: str


class KimiAdapter:
    """Locates a local Kimi CLI session and a usable credential."""

    name = "kimi"

    def __init__(self, *, kimi_home: Path | None = None) -> None:
        self._kimi_home = kimi_home if kimi_home is not None else _default_kimi_home()

    def locate_session(self, *, session_ref: str | None = None) -> LocatedSession:
        session_file = _find_session_file(self._kimi_home, session_ref)
        session_id = validate_identifier(session_file.session_id, what="session id")
        credential = _resolve_credential(self._kimi_home)

        home_dir_name = self._kimi_home.name
        sandbox_path = f"{SANDBOX_HOME}/{home_dir_name}/sessions/{session_id}"

        return LocatedSession(
            tool=self.name,
            session_id=session_id,
            files=(SessionFile(local_path=session_file.path, sandbox_path=sandbox_path),),
            credential=credential,
            resume_command=f"kimi --session {session_id}",
            workdir=SANDBOX_HOME,
        )


def _default_kimi_home() -> Path:
    override = (
        os.environ.get("KIMI_CODE_HOME", "").strip() or os.environ.get("KIMI_CLI_HOME", "").strip()
    )
    if override:
        return Path(override)

    home = Path.home()
    if (home / KIMI_CODE_HOME_DIR_NAME).is_dir():
        return home / KIMI_CODE_HOME_DIR_NAME
    if (home / KIMI_LEGACY_HOME_DIR_NAME).is_dir():
        return home / KIMI_LEGACY_HOME_DIR_NAME

    return home / KIMI_CODE_HOME_DIR_NAME


def _find_session_file(kimi_home: Path, session_ref: str | None) -> _KimiSessionFile:
    sessions_root = kimi_home / "sessions"
    if not sessions_root.is_dir():
        raise HandoffError(
            f"No Kimi sessions directory found under {kimi_home}. Run Kimi CLI at "
            "least once locally before handing off a session."
        )

    candidate_dirs = [p for p in sessions_root.iterdir() if p.is_dir()]

    if not candidate_dirs:
        raise HandoffError(f"No local Kimi sessions found under {sessions_root}.")
    candidates = [_KimiSessionFile(path=p, session_id=p.name) for p in candidate_dirs]

    if session_ref is not None:
        matches = [
            c for c in candidates if c.session_id == session_ref or c.path.stem == session_ref
        ]
        if not matches:
            raise HandoffError(
                f"No local Kimi session file found for session id {session_ref!r} "
                f"under {sessions_root}."
            )
        return matches[0]

    most_recent_path = most_recent_by_mtime(c.path for c in candidates)
    return next(c for c in candidates if c.path == most_recent_path)


def _resolve_credential(kimi_home: Path) -> Credential:
    """Resolve the portable credential Kimi CLI should authenticate with."""
    for env_var in ("KIMI_API_KEY", "MOONSHOT_API_KEY"):
        key = os.environ.get(env_var, "").strip()
        if key:
            return Credential(env_var=env_var, value=key)

    raise HandoffError(
        "No usable Kimi credential found: set KIMI_API_KEY or MOONSHOT_API_KEY in your environment."
    )
