"""Cline CLI handoff adapter.

Verified on 2026-11-16 against `cline --version` 3.0.74 (npm `cline`, macOS
arm64) with an isolated `HOME`, by round trips against a local fake
OpenAI-compatible model server that logged every request. The binary is
compiled, so every fact below was observed rather than read from source:

- Each session is a directory `~/.cline/data/sessions/<id>/` holding
  `<id>.json` (metadata, including the session's `provider` and an absolute
  `messages_path`) and `<id>.messages.json` (the conversation). The
  `db/sessions.db` index is not needed.
- `cline --data-dir <dir> --id <id>` resumes in the interactive UI only, so it
  needs a terminal, which the takeover shell is. A fresh data dir holding just
  the two session files and the provider settings resumed from another
  directory with the whole earlier conversation, even with the recorded `cwd`
  pointing at a nonexistent path.
- `messages_path` must point at the pushed messages file. Left pointing at the
  original machine, Cline silently starts from an empty history.
- A compacted session also has `<id>.compaction.json`, found through an
  absolute `compaction_path` in `<id>.json` (from the bundled source).
- The provider key is read only from `settings/providers.json`; no
  environment variable replaced it. Per the bundled schema, `tokenSource` is
  `manual` (the default when absent), `migration` (imported from older Cline,
  usually API keys) or `oauth`; account logins are `oauth` or carry
  `settings.auth` access/refresh tokens, which are not portable.
- Ctrl-C exits Cline normally, so a command chained after it still runs.

The key file must stay out of `/workspace`, and `--data-dir` cannot split
settings from sessions, so the whole data dir, resumed session included,
lives under `/tmp`.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ..core import HandoffError, LocatedSession, SessionFile, most_recent_by_mtime, validate_identifier

SANDBOX_HOME = "/workspace"
SANDBOX_DATA_DIR = "/tmp/.boxxkite-handoff-cline"
SANDBOX_PROVIDERS_PATH = f"{SANDBOX_DATA_DIR}/settings/providers.json"
MARKER_NAME = ".boxxkite-handoff"


class ClineAdapter:
    """Locate a resumable Cline CLI session and the provider settings it runs with."""

    name = "cline"

    def __init__(self, *, data_dir: Path | None = None) -> None:
        self._data_dir = data_dir or Path.home() / ".cline" / "data"

    def locate_session(self, *, session_ref: str | None = None) -> LocatedSession:
        meta_path = _find_session_meta(self._data_dir, session_ref)
        session_id = validate_identifier(meta_path.parent.name, what="session id")
        meta = _read_json(meta_path)
        sandbox_session_dir = f"{SANDBOX_DATA_DIR}/sessions/{session_id}"
        meta["messages_path"] = f"{sandbox_session_dir}/{session_id}.messages.json"
        session_files = [
            SessionFile(local_path=meta_path.with_name(f"{session_id}.messages.json"), sandbox_path=meta["messages_path"])
        ]
        compaction = meta_path.with_name(f"{session_id}.compaction.json")
        if compaction.is_file():
            meta["compaction_path"] = f"{sandbox_session_dir}/{compaction.name}"
            session_files.append(SessionFile(local_path=compaction, sandbox_path=meta["compaction_path"]))
        else:
            meta.pop("compaction_path", None)
        providers = _scoped_providers(self._data_dir / "settings" / "providers.json", meta)

        tmp_dir = Path(tempfile.mkdtemp(prefix="boxxkite-handoff-cline-"))
        local_meta = tmp_dir / meta_path.name
        local_meta.write_text(json.dumps(meta), encoding="utf-8")
        local_providers = tmp_dir / "providers.json"
        local_providers.write_text(json.dumps(providers), encoding="utf-8")
        marker = tmp_dir / "marker"
        marker.write_text("", encoding="utf-8")

        return LocatedSession(
            tool=self.name,
            session_id=session_id,
            files=(
                # file_create hands only a pushed file's own directory to the sandbox user, and
                # Cline must create db/, logs/ and new sessions beside what is pushed.
                SessionFile(local_path=marker, sandbox_path=f"{SANDBOX_DATA_DIR}/{MARKER_NAME}"),
                SessionFile(local_path=marker, sandbox_path=f"{SANDBOX_DATA_DIR}/sessions/{MARKER_NAME}"),
                SessionFile(local_path=local_meta, sandbox_path=f"{sandbox_session_dir}/{session_id}.json"),
                *session_files,
                SessionFile(local_path=local_providers, sandbox_path=SANDBOX_PROVIDERS_PATH),
            ),
            credential=None,
            resume_command=(
                f"cline --data-dir {SANDBOX_DATA_DIR} --id {session_id}; rm -f {SANDBOX_PROVIDERS_PATH}"
            ),
            workdir=SANDBOX_HOME,
            cleanup=lambda: shutil.rmtree(tmp_dir, ignore_errors=True),
        )


def _find_session_meta(data_dir: Path, session_ref: str | None) -> Path:
    sessions_dir = data_dir / "sessions"
    if session_ref is not None:
        session_ref = validate_identifier(session_ref, what="session id")
        meta_path = sessions_dir / session_ref / f"{session_ref}.json"
        if not _is_session(meta_path):
            raise HandoffError(f"No local Cline session found for id {session_ref}.")
        return meta_path
    candidates = [
        path / f"{path.name}.json"
        for path in sessions_dir.glob("*")
        if _is_session(path / f"{path.name}.json")
    ]
    if not candidates:
        raise HandoffError(
            f"No resumable Cline session found under {sessions_dir}. "
            "Run cline locally before handing off a session."
        )
    return most_recent_by_mtime(candidates)


def _is_session(meta_path: Path) -> bool:
    messages_path = meta_path.with_name(f"{meta_path.parent.name}.messages.json")
    return meta_path.is_file() and messages_path.is_file()


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HandoffError(f"Could not read {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise HandoffError(f"{path} is not a JSON object.")
    return data


def _scoped_providers(providers_path: Path, meta: dict[str, Any]) -> dict[str, Any]:
    provider = meta.get("provider")
    if not isinstance(provider, str):
        raise HandoffError("The Cline session does not record which provider it used.")
    if not providers_path.is_file():
        raise HandoffError(
            f"No Cline provider settings at {providers_path}. "
            "Run `cline auth` with an API key before handing off."
        )
    settings = _read_json(providers_path)
    all_providers = settings.get("providers")
    entry = all_providers.get(provider) if isinstance(all_providers, dict) else None
    if not isinstance(entry, dict):
        raise HandoffError(f"{providers_path} has no settings for the session's provider {provider!r}.")
    auth = (entry.get("settings") or {}).get("auth")
    has_login_tokens = isinstance(auth, dict) and ("accessToken" in auth or "refreshToken" in auth)
    if entry.get("tokenSource") == "oauth" or has_login_tokens:
        raise HandoffError(
            f"The session's provider {provider!r} is signed in with an account login, which "
            "cannot be carried into the sandbox. Hand off a session that uses an API-key provider."
        )
    return {"version": settings.get("version", 1), "lastUsedProvider": provider, "providers": {provider: entry}}
