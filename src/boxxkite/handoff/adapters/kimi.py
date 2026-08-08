"""Kimi CLI adapter handoff

Locates local Kimi Code CLI sessions and prepares them for resumption in a boxxkite
sandbox.

Kimi Code CLI stores session data under ``$KIMI_CODE_HOME/sessions/<project_bucket>/<session_id>/``
(``KIMI_CODE_HOME`` defaults to ``~/.kimi-code``, or fallback ``~/.kimi`` for older CLI versions).
Credentials are read directly from the CLI's config.toml.
"""

from __future__ import annotations

import os
import tomllib
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
        # Запазваме оригиналната "кофа" (bucket) на проекта, която Kimi е генерирал
        bucket_name = session_file.path.parent.name
        sandbox_path = f"{SANDBOX_HOME}/{home_dir_name}/sessions/{bucket_name}/{session_id}"

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

    candidate_dirs: list[Path] = []
    for bucket_dir in sessions_root.iterdir():
        if bucket_dir.is_dir():
            candidate_dirs.extend([p for p in bucket_dir.iterdir() if p.is_dir()])

    if not candidate_dirs:
        raise HandoffError(f"No local Kimi sessions found under {sessions_root}/*/")

    candidates = [_KimiSessionFile(path=p, session_id=p.name) for p in candidate_dirs]

    if session_ref is not None:
        matches = [
            c for c in candidates if c.session_id == session_ref or c.path.stem == session_ref
        ]
        if not matches:
            raise HandoffError(
                f"No local Kimi session file found for session id {session_ref!r} "
                f"under {sessions_root}/*/"
            )
        return matches[0]

    most_recent_path = most_recent_by_mtime(c.path for c in candidates)
    return next(c for c in candidates if c.path == most_recent_path)


def _resolve_credential(kimi_home: Path) -> Credential:
    """Resolve the portable credential Kimi CLI uses from config.toml."""
    config_path = kimi_home / "config.toml"
    if not config_path.is_file():
        raise HandoffError(f"No Kimi config found at {config_path}")

    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception as exc:
        raise HandoffError(f"Failed to read Kimi config at {config_path}: {exc}") from exc

    try:
        provider_cfg = config.get("providers", {}).get("managed:kimi-code", {})
        key = provider_cfg.get("api_key") or provider_cfg.get("token")
    except AttributeError:
        key = None

    if not key or not str(key).strip():
        raise HandoffError(
            f'No usable Kimi credential found in {config_path} under [providers."managed:kimi-code"]'
        )

    return Credential(env_var="KIMI_API_KEY", value=str(key).strip())
