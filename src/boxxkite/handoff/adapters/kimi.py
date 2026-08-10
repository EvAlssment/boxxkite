"""Kimi Code CLI handoff adapter.

Locates a local Kimi Code CLI session and rebuilds everything it needs to
resume correctly in a fresh boxxkite sandbox. Every detail below was verified
directly against a real installed ``@moonshot-ai/kimi-code`` (v0.34.0) binary
by reading its bundled source AND by round-tripping a real session end to
end -- created one for real (a fake API key gets far enough to write real
session state before the model call itself fails on auth), copied it into a
from-scratch home directory simulating a fresh sandbox, and confirmed
``kimi --session <id>`` resumes it once (and only once) every detail below is
handled. This adapter went through two earlier, narrower passes that each
missed part of this -- see PR history for what those got wrong.

Three things make Kimi meaningfully different from every other adapter in
this package:

1. **A session is a whole directory, not one file.** It contains at least
   ``state.json`` and ``agents/main/wire.jsonl`` (plus ``logs/``,
   ``context.jsonl``, or ``media-originals/`` depending on what the
   conversation actually did). Every real file under the session directory
   has to be pushed, not just one.
2. **Resume is index-based, not directory-scan-based.** ``kimi --session
   <id>`` looks ``<id>`` up in ``$KIMI_CODE_HOME/session_index.jsonl`` (a
   flat, home-dir-level ``{"sessionId", "sessionDir", "workDir"}`` JSONL
   file) and throws ``Session "<id>" not found`` if there's no entry --
   confirmed directly in the bundled source's ``resumeById``/
   ``findExistingSessionEntry``, no directory-tree fallback exists. A fresh
   sandbox has never written this file, so a synthesized single-entry
   version has to be pushed alongside the session directory itself.
3. **The session's original working directory is baked into more than one
   place, and checked.** ``kimi --session <id>`` rejects the resume outright
   ("Session ... was created under a different directory") if
   ``state.json``'s own ``cwd`` field doesn't match the *current* directory
   (source: the ``opts.session !== void 0`` branch in the CLI's run command,
   comparing ``resolve(target.cwd) !== resolve(workDir)``). The session's
   containing directory name also has to be exactly the bucket key
   ``kimi`` itself would compute for the destination directory --
   ``wd_<basename>_<sha256(realpath(dir)).hexdigest()[:12]>`` (verified
   byte-for-byte against a real bucket directory name kimi created on disk).
   Both need rewriting to the sandbox's own ``/workspace``, not copied as-is
   from the source machine.

Credentials are a fourth difference: real Kimi Code CLI does not read an API
key from the shell environment for actual model calls at all -- confirmed
live, setting ``KIMI_API_KEY`` in a completely fresh, unconfigured home
directory still failed with "No model configured... or set default_model in
config.toml". The only real credential source is ``config.toml`` itself
(``default_model = "<provider>/<model>"`` plus that provider's own
``[providers.<provider>]`` block, wherever it was actually configured --
which is *not* always ``managed:kimi-code``; a manually added or
catalog-imported provider, e.g. ``moonshotai``, is just as valid and was
exactly what this adapter was tested against). So this adapter doesn't use
this package's ``Credential``/env-var-export mechanism at all -- it instead
pushes a small, scoped ``config.toml`` (just the active provider's own block
plus its model definitions, not the user's entire file) as one more ordinary
pushed file. This is not a weaker guarantee than the `Credential` path: both
ultimately go through the same `file_create` call, whose own control-plane
audit entry records only `{"path": ...}`, never file content -- see
`orchestrator.py`'s module docstring for the full audit-log threat model
this relies on.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import tomllib
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import tomli_w

from ..core import HandoffError, LocatedSession, SessionFile, most_recent_by_mtime, validate_identifier

SANDBOX_HOME = "/workspace"
KIMI_CODE_HOME_DIR_NAME = ".kimi-code"
KIMI_LEGACY_HOME_DIR_NAME = ".kimi"
SESSION_INDEX_FILENAME = "session_index.jsonl"
STATE_FILENAME = "state.json"
CONFIG_FILENAME = "config.toml"
# Confirmed against a real bucket directory name kimi itself created
# (`wd_project_3ca089123ce9` for a workdir whose sha256 hexdigest starts
# with exactly those 12 characters).
BUCKET_HASH_LENGTH = 12


@dataclass(frozen=True)
class _KimiSession:
    session_dir: Path
    session_id: str


class KimiAdapter:
    """Locates a local Kimi Code CLI session and rebuilds it (directory
    tree, index entry, and scoped credential) for resumption in a sandbox
    whose workdir is always `/workspace`. See this module's docstring for
    why all three of those are necessary, not just the session directory."""

    name = "kimi"

    def __init__(self, *, kimi_home: Path | None = None) -> None:
        self._kimi_home = kimi_home if kimi_home is not None else _default_kimi_home()

    def locate_session(self, *, session_ref: str | None = None) -> LocatedSession:
        session = _find_session(self._kimi_home, session_ref)
        session_id = validate_identifier(session.session_id, what="session id")

        sandbox_bucket = _bucket_name_for(SANDBOX_HOME)
        sandbox_session_dir = f"{SANDBOX_HOME}/{KIMI_CODE_HOME_DIR_NAME}/sessions/{sandbox_bucket}/{session_id}"

        files: list[SessionFile] = []
        cleanups: list[Callable[[], None]] = []

        for path in sorted(session.session_dir.rglob("*")):
            if not path.is_file():
                continue
            rel = path.relative_to(session.session_dir).as_posix()
            sandbox_path = f"{sandbox_session_dir}/{rel}"
            if path.name == STATE_FILENAME:
                local_path, cleanup = _rewrite_state_json(path, new_cwd=SANDBOX_HOME, new_session_dir=sandbox_session_dir)
                cleanups.append(cleanup)
            else:
                local_path = path
            files.append(SessionFile(local_path=local_path, sandbox_path=sandbox_path))

        index_path, index_cleanup = _write_session_index(session_id, session_dir=sandbox_session_dir, work_dir=SANDBOX_HOME)
        cleanups.append(index_cleanup)
        files.append(
            SessionFile(
                local_path=index_path,
                sandbox_path=f"{SANDBOX_HOME}/{KIMI_CODE_HOME_DIR_NAME}/{SESSION_INDEX_FILENAME}",
            )
        )

        config_path, config_cleanup = _write_scoped_config(self._kimi_home)
        cleanups.append(config_cleanup)
        files.append(
            SessionFile(
                local_path=config_path,
                sandbox_path=f"{SANDBOX_HOME}/{KIMI_CODE_HOME_DIR_NAME}/{CONFIG_FILENAME}",
            )
        )

        def cleanup_all() -> None:
            for c in cleanups:
                c()

        return LocatedSession(
            tool=self.name,
            session_id=session_id,
            files=tuple(files),
            credential=None,
            resume_command=f"kimi --session {session_id}",
            workdir=SANDBOX_HOME,
            cleanup=cleanup_all,
        )


def _default_kimi_home() -> Path:
    override = os.environ.get("KIMI_CODE_HOME", "").strip() or os.environ.get("KIMI_CLI_HOME", "").strip()
    if override:
        return Path(override)

    home = Path.home()
    if (home / KIMI_CODE_HOME_DIR_NAME).is_dir():
        return home / KIMI_CODE_HOME_DIR_NAME
    if (home / KIMI_LEGACY_HOME_DIR_NAME).is_dir():
        return home / KIMI_LEGACY_HOME_DIR_NAME
    return home / KIMI_CODE_HOME_DIR_NAME


def _bucket_name_for(directory: str) -> str:
    """Reproduces kimi-code's own `encodeWorkDirKey` exactly: `wd_` + the
    directory's basename + `_` + the first 12 hex chars of the sha256 of its
    *resolved* (symlink-free) absolute path. Verified byte-for-byte against
    a real bucket directory kimi created on disk for a known path."""
    resolved = str(Path(directory).resolve())
    digest = hashlib.sha256(resolved.encode()).hexdigest()[:BUCKET_HASH_LENGTH]
    return f"wd_{Path(resolved).name}_{digest}"


def _find_session(kimi_home: Path, session_ref: str | None) -> _KimiSession:
    sessions_root = kimi_home / "sessions"
    if not sessions_root.is_dir():
        raise HandoffError(
            f"No Kimi sessions directory found under {kimi_home}. Run Kimi CLI at "
            "least once locally before handing off a session."
        )

    candidates: list[_KimiSession] = []
    for bucket_dir in sessions_root.iterdir():
        if not bucket_dir.is_dir():
            continue
        for session_dir in bucket_dir.iterdir():
            if session_dir.is_dir() and (session_dir / STATE_FILENAME).is_file():
                candidates.append(_KimiSession(session_dir=session_dir, session_id=session_dir.name))

    if not candidates:
        raise HandoffError(f"No local Kimi sessions found under {sessions_root}/*/")

    if session_ref is not None:
        matches = [c for c in candidates if c.session_id == session_ref]
        if not matches:
            raise HandoffError(f"No local Kimi session found for session id {session_ref!r} under {sessions_root}/*/")
        return matches[0]

    most_recent_dir = most_recent_by_mtime(c.session_dir for c in candidates)
    return next(c for c in candidates if c.session_dir == most_recent_dir)


def _rewrite_state_json(path: Path, *, new_cwd: str, new_session_dir: str) -> tuple[Path, Callable[[], None]]:
    """state.json bakes in the session's original, absolute cwd and each
    agent's absolute homedir -- both have to point at the sandbox's own
    paths, or kimi refuses to resume ("was created under a different
    directory") even once the file itself is in the right place."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HandoffError(f"Could not read {path}: {exc}") from exc

    data["cwd"] = new_cwd
    for agent in data.get("agents", {}).values():
        if isinstance(agent, dict) and "homedir" in agent:
            agent_id = Path(agent["homedir"]).name
            agent["homedir"] = f"{new_session_dir}/agents/{agent_id}"

    tmp_dir = Path(tempfile.mkdtemp(prefix="boxxkite-handoff-kimi-"))
    tmp_path = tmp_dir / STATE_FILENAME
    tmp_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return tmp_path, lambda: shutil.rmtree(tmp_dir, ignore_errors=True)


def _write_session_index(session_id: str, *, session_dir: str, work_dir: str) -> tuple[Path, Callable[[], None]]:
    """A single-entry session_index.jsonl -- the *only* thing `kimi
    --session <id>` consults to find a session by id, see this module's
    docstring. A fresh sandbox has never written this file at all."""
    record = {"sessionId": session_id, "sessionDir": session_dir, "workDir": work_dir}
    tmp_dir = Path(tempfile.mkdtemp(prefix="boxxkite-handoff-kimi-index-"))
    tmp_path = tmp_dir / SESSION_INDEX_FILENAME
    tmp_path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return tmp_path, lambda: shutil.rmtree(tmp_dir, ignore_errors=True)


def _write_scoped_config(kimi_home: Path) -> tuple[Path, Callable[[], None]]:
    """A minimal config.toml: just `default_model` plus that model's own
    provider block and every `[models.*]` entry belonging to it -- not the
    user's whole config file, which may hold other providers' keys this
    session has no business copying. Pushed as a plain file rather than
    through this package's Credential/env-var mechanism because real Kimi
    Code CLI has no env-var auth path at all (verified live -- see module
    docstring); the credential only ever exists inside config.toml itself.
    """
    config_path = kimi_home / CONFIG_FILENAME
    if not config_path.is_file():
        raise HandoffError(
            f"No Kimi config found at {config_path}. Run `kimi` and complete /login, or "
            "`kimi provider add`/`kimi provider catalog add`, before handing off a session."
        )

    try:
        with config_path.open("rb") as f:
            config = tomllib.load(f)
    except Exception as exc:
        raise HandoffError(f"Failed to read Kimi config at {config_path}: {exc}") from exc

    default_model = config.get("default_model")
    if not isinstance(default_model, str) or "/" not in default_model:
        raise HandoffError(f"No usable default_model set in {config_path} -- run `kimi` and pick a model first.")
    provider_id = default_model.split("/", 1)[0]

    providers = config.get("providers", {})
    provider_block = providers.get(provider_id)
    if not isinstance(provider_block, dict):
        raise HandoffError(
            f"default_model {default_model!r} references provider {provider_id!r}, which has no "
            f"entry under [providers] in {config_path}."
        )

    scoped: dict = {"default_model": default_model, "providers": {provider_id: provider_block}}
    models = {
        key: value
        for key, value in config.get("models", {}).items()
        if isinstance(value, dict) and value.get("provider") == provider_id
    }
    if models:
        scoped["models"] = models

    tmp_dir = Path(tempfile.mkdtemp(prefix="boxxkite-handoff-kimi-config-"))
    tmp_path = tmp_dir / CONFIG_FILENAME
    tmp_path.write_bytes(tomli_w.dumps(scoped).encode("utf-8"))
    return tmp_path, lambda: shutil.rmtree(tmp_dir, ignore_errors=True)
