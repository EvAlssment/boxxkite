"""Qwen Code handoff adapter.

Qwen Code records project-scoped conversations as UUID-named JSONL files
under ``$QWEN_RUNTIME_DIR/projects/<project-id>/chats``.  When no runtime
override is configured, ``QWEN_HOME`` (default ``~/.qwen``) is the runtime
root.  These paths and ``qwen --resume <session-id>`` are verified against
Qwen Code's current ``Storage``, ``ChatRecordingService``, and CLI config.
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..core import Credential, HandoffError, LocatedSession, SessionFile, most_recent_by_mtime

SANDBOX_HOME = "/workspace"
SANDBOX_PROJECT_ID = "-workspace"


@dataclass(frozen=True)
class _QwenSessionFile:
    path: Path
    session_id: str


class QwenAdapter:
    """Locate a resumable Qwen Code session and portable API credential."""

    name = "qwen"

    def __init__(self, *, qwen_runtime_dir: Path | None = None) -> None:
        self._runtime_dir = qwen_runtime_dir or _default_runtime_dir()

    def locate_session(self, *, session_ref: str | None = None) -> LocatedSession:
        session = _find_session_file(self._runtime_dir, session_ref)
        credential = _resolve_credential()
        sandbox_path = (
            f"{SANDBOX_HOME}/.qwen/projects/{SANDBOX_PROJECT_ID}/chats/"
            f"{session.session_id}.jsonl"
        )
        return LocatedSession(
            tool=self.name,
            session_id=session.session_id,
            files=(SessionFile(local_path=session.path, sandbox_path=sandbox_path),),
            credential=credential,
            resume_command=f"qwen --resume {session.session_id}",
            workdir=SANDBOX_HOME,
        )


def _default_runtime_dir() -> Path:
    runtime_override = os.environ.get("QWEN_RUNTIME_DIR", "").strip()
    if runtime_override:
        return Path(runtime_override).expanduser()
    home_override = os.environ.get("QWEN_HOME", "").strip()
    if home_override:
        return Path(home_override).expanduser()
    return Path.home() / ".qwen"


def _read_session_id(file_path: Path) -> str | None:
    try:
        with file_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session_id = record.get("sessionId") if isinstance(record, dict) else None
                if isinstance(session_id, str):
                    try:
                        return str(uuid.UUID(session_id))
                    except ValueError:
                        return None
    except OSError as exc:
        raise HandoffError(f"Could not read Qwen session file at {file_path}: {exc}") from exc
    return None


def _find_session_file(runtime_dir: Path, session_ref: str | None) -> _QwenSessionFile:
    projects_dir = runtime_dir / "projects"
    files = list(projects_dir.glob("*/chats/*.jsonl")) if projects_dir.is_dir() else []
    candidates = [
        _QwenSessionFile(path=file_path, session_id=session_id)
        for file_path in files
        if file_path.is_file() and (session_id := _read_session_id(file_path)) is not None
    ]
    if not candidates:
        raise HandoffError(
            f"No resumable Qwen Code session files found under {projects_dir}. "
            "Run Qwen Code with chat recording enabled before handing off a session."
        )
    if session_ref is not None:
        try:
            wanted = str(uuid.UUID(session_ref))
        except ValueError as exc:
            raise HandoffError(f"Qwen session id {session_ref!r} is not a valid UUID.") from exc
        matches = [candidate for candidate in candidates if candidate.session_id == wanted]
        if not matches:
            raise HandoffError(f"No local Qwen Code session found for id {wanted}.")
        return matches[0]
    newest = most_recent_by_mtime(candidate.path for candidate in candidates)
    return next(candidate for candidate in candidates if candidate.path == newest)


def _resolve_credential() -> Credential:
    for env_var in ("DASHSCOPE_API_KEY", "OPENAI_API_KEY"):
        value = os.environ.get(env_var, "").strip()
        if value:
            return Credential(env_var=env_var, value=value)
    raise HandoffError(
        "No portable Qwen credential found: set DASHSCOPE_API_KEY for the Qwen provider "
        "mode or OPENAI_API_KEY for an OpenAI-compatible provider."
    )
