"""Continue CLI (`cn`) handoff adapter.

Verified on 2026-11-16 against `cn --version` 1.5.47 (npm
`@continuedev/cli`, macOS arm64), by reading its shipped source
(`session.ts`, `configLoader.ts`, `CLIPlatformClient.ts`, `onboarding.ts`,
`services/ModelService.ts`) and by round trips against a local fake
OpenAI-compatible model server that logged every request:

- Each session is one file, `$CONTINUE_GLOBAL_DIR/sessions/<uuid>.json`.
  `sessions/sessions.json` is an index, not a session.
- `cn --resume` takes no id: it loads the newest `*.json` in `sessions/`. The
  sandbox holds only the pushed file, so that is the one it loads. Copied into
  a fresh home and resumed from another directory, with the recorded
  `workspaceDirectory` pointing at a nonexistent path, the next model request
  carried the whole earlier conversation.
- There is no login token: 1.5.47 removed Hub auth. Model keys live in
  `config.yaml`, as literals or as `${{ secrets.NAME }}`, which `cn` resolves
  from the process environment first. The active model is the chat model
  named by `cliSelectedModel` in `index/globalContext.json`, else the first
  chat model.
- `--config <path>` skips the interactive first-run onboarding prompt, which
  otherwise asks for an Anthropic key in a fresh home.

So the adapter pushes a config holding only the active model, with its
secrets filled in, the same scoping kimi.py applies to its provider config.
Rules, MCP servers and other config sections are not carried.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import yaml

from ..core import HandoffError, LocatedSession, SessionFile, most_recent_by_mtime, validate_identifier

SANDBOX_HOME = "/workspace"
SANDBOX_SECRET_CONFIG_PATH = "/tmp/.boxxkite-handoff-continue-config.yaml"
_TEMPLATE_RE = re.compile(r"\$\{\{\s*([^}\s]+)\s*\}\}")


class ContinueAdapter:
    """Locate a resumable Continue CLI session and the model config it runs with."""

    name = "continue"

    def __init__(self, *, continue_home: Path | None = None) -> None:
        self._home = continue_home or _default_home()

    def locate_session(self, *, session_ref: str | None = None) -> LocatedSession:
        session_path = _find_session_file(self._home, session_ref)
        session_id = validate_identifier(session_path.stem, what="session id")
        config_path, cleanup = _write_scoped_config(self._home)
        return LocatedSession(
            tool=self.name,
            session_id=session_id,
            files=(
                SessionFile(
                    local_path=session_path,
                    sandbox_path=f"{SANDBOX_HOME}/.continue/sessions/{session_id}.json",
                ),
                SessionFile(local_path=config_path, sandbox_path=SANDBOX_SECRET_CONFIG_PATH),
            ),
            credential=None,
            resume_command=(
                f"cn --config {SANDBOX_SECRET_CONFIG_PATH} --resume; "
                f"rm -f {SANDBOX_SECRET_CONFIG_PATH}"
            ),
            workdir=SANDBOX_HOME,
            cleanup=cleanup,
        )


def _default_home() -> Path:
    override = os.environ.get("CONTINUE_GLOBAL_DIR", "").strip()
    return Path(override).expanduser() if override else Path.home() / ".continue"


def _is_canonical_uuid(name: str) -> bool:
    try:
        return str(uuid.UUID(name)) == name
    except ValueError:
        return False


def _find_session_file(home: Path, session_ref: str | None) -> Path:
    sessions_dir = home / "sessions"
    candidates = [
        path
        for path in sessions_dir.glob("*.json")
        if path.is_file() and _is_canonical_uuid(path.stem)
    ]
    if not candidates:
        raise HandoffError(
            f"No resumable Continue CLI session found under {sessions_dir}. "
            "Run cn locally before handing off a session."
        )
    if session_ref is None:
        return most_recent_by_mtime(candidates)
    try:
        wanted = str(uuid.UUID(session_ref))
    except ValueError as exc:
        raise HandoffError(f"Continue session id {session_ref!r} is not a valid UUID.") from exc
    session_path = sessions_dir / f"{wanted}.json"
    if not session_path.is_file():
        raise HandoffError(f"No local Continue CLI session found for id {wanted}.")
    return session_path


def _write_scoped_config(home: Path) -> tuple[Path, Callable[[], None]]:
    config_path = home / "config.yaml"
    if not config_path.is_file():
        raise HandoffError(
            f"No local Continue config at {config_path}. Handoff needs a local config.yaml "
            "that defines the model and its key."
        )
    try:
        config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise HandoffError(f"Could not read Continue config at {config_path}: {exc}") from exc
    if not isinstance(config, dict):
        raise HandoffError(f"Continue config at {config_path} is not a YAML mapping.")

    model = _render_secrets(_active_model(home, config, config_path), config_path)
    scoped = {key: config[key] for key in ("name", "version", "schema") if key in config}
    scoped["models"] = [model]

    tmp_dir = Path(tempfile.mkdtemp(prefix="boxxkite-handoff-continue-"))
    scoped_path = tmp_dir / "config.yaml"
    scoped_path.write_text(yaml.safe_dump(scoped, sort_keys=False), encoding="utf-8")
    return scoped_path, lambda: shutil.rmtree(tmp_dir, ignore_errors=True)


def _active_model(home: Path, config: dict[str, Any], config_path: Path) -> dict[str, Any]:
    models, seen_names = [], set()
    for model in config.get("models") or []:
        if not isinstance(model, dict):
            continue
        name = model.get("name")
        # cn keeps only the first model with a given name, before it filters by role.
        if isinstance(name, str):
            if name in seen_names:
                continue
            seen_names.add(name)
        models.append(model)
    if any("uses" in m for m in models):
        raise HandoffError(
            f"{config_path} pulls a model block with `uses:`, which handoff does not carry. "
            "Inline the model's provider, model and key, then retry."
        )
    chat_models = [
        m for m in models if "roles" not in m or "chat" in (m.get("roles") or [])
    ]
    if not chat_models:
        raise HandoffError(f"{config_path} defines no chat model.")
    selected = _persisted_model_name(home)
    for model in chat_models:
        if selected is not None and (model.get("name") or model.get("model")) == selected:
            return model
    return chat_models[0]


def _persisted_model_name(home: Path) -> str | None:
    try:
        context = json.loads((home / "index" / "globalContext.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    name = context.get("cliSelectedModel") if isinstance(context, dict) else None
    return name if isinstance(name, str) else None


def _render_secrets(value: Any, config_path: Path) -> Any:
    if isinstance(value, dict):
        return {key: _render_secrets(item, config_path) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_secrets(item, config_path) for item in value]
    if isinstance(value, str):
        return _TEMPLATE_RE.sub(lambda match: _secret_value(match.group(1), config_path), value)
    return value


def _secret_value(reference: str, config_path: Path) -> str:
    secret_path = reference.removeprefix("secrets.")
    if secret_path == reference:
        raise HandoffError(
            f"{config_path} uses the template ${{{{ {reference} }}}}, which handoff cannot resolve."
        )
    name = secret_path.rsplit("/", 1)[-1]
    value = os.environ.get(name, "").strip()
    if not value:
        raise HandoffError(
            f"{config_path} references ${{{{ secrets.{secret_path} }}}}, but {name} is not set in "
            "this shell. Export it, then retry."
        )
    return value
