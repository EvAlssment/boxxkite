"""Gemini CLI handoff adapter.

Locates local Gemini CLI sessions and prepares them for resumption in a boxxkite
sandbox.

Gemini CLI stores session logs under ``$GEMINI_CLI_HOME/tmp/<project-slug>/chats/session-*.jsonl``
(or ``~/.gemini-cli/tmp/...``).
Credentials are read from the ``GEMINI_API_KEY`` environment variable or from
``$GEMINI_CLI_HOME/settings.json``.
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
GEMINI_CLI_HOME_DIR_NAME = ".gemini-cli"


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

        # Calculate relative path from local gemini home to maintain structure in sandbox
        try:
            rel_path = session_file.path.relative_to(self._gemini_home)
        except ValueError:
            rel_path = session_file.path.name

        sandbox_path = f"{SANDBOX_HOME}/{GEMINI_CLI_HOME_DIR_NAME}/{rel_path}"

        return LocatedSession(
            tool=self.name,
            session_id=session_id,
            files=(SessionFile(local_path=session_file.path, sandbox_path=sandbox_path),),
            credential=credential,
            resume_command=f"gemini --resume {session_id}",
            workdir=SANDBOX_HOME,
        )


def _default_gemini_home() -> Path:
    override = os.environ.get("GEMINI_CLI_HOME", "").strip()
    if override:
        return Path(override)
    return Path.home() / GEMINI_CLI_HOME_DIR_NAME


def _parse_session_details(file_path: Path) -> tuple[str, bool]:
    """Extract full sessionId and check if there is at least one completed assistant turn."""
    session_id = None
    has_completed_assistant_turn = False

    try:
        with file_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if not session_id and isinstance(data, dict):
                        session_id = data.get("sessionId")

                    role = data.get("role") or (
                        data.get("message", {}).get("role")
                        if isinstance(data.get("message"), dict)
                        else None
                    )
                    if role == "assistant":
                        has_completed_assistant_turn = True
                except json.JSONDecodeError:
                    continue
    except OSError as exc:
        raise HandoffError(f"Could not read session file at {file_path}: {exc}") from exc

    if not session_id:
        session_id = file_path.stem

    return session_id, has_completed_assistant_turn


def _find_session_file(gemini_home: Path, session_ref: str | None) -> _GeminiSessionFile:
    tmp_root = gemini_home / "tmp"
    if not tmp_root.is_dir():
        raise HandoffError(
            f"No Gemini sessions directory found under {gemini_home}. Run Gemini CLI at "
            "least once locally before handing off a session."
        )

    jsonl_files = list(tmp_root.glob("**/chats/session-*.jsonl"))
    if not jsonl_files:
        # Fallback to any jsonl under tmp if directory structure varies
        jsonl_files = list(tmp_root.glob("**/*.jsonl"))

    if not jsonl_files:
        raise HandoffError(f"No local Gemini session files (*.jsonl) found under {tmp_root}.")

    candidates: list[_GeminiSessionFile] = []
    for p in jsonl_files:
        if not p.is_file():
            continue
        session_id, has_assistant_turn = _parse_session_details(p)
        if not has_assistant_turn:
            # Skip sessions with no completed assistant turns as gemini --resume fails on them
            continue
        candidates.append(_GeminiSessionFile(path=p, session_id=session_id))

    if not candidates:
        raise HandoffError(
            f"No valid Gemini sessions with completed assistant turns found under {tmp_root}."
        )

    if session_ref is not None:
        matches = [
            c for c in candidates if c.session_id == session_ref or c.path.stem == session_ref
        ]
        if not matches:
            raise HandoffError(
                f"No local Gemini session file found for session id {session_ref!r} "
                f"under {tmp_root}."
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
