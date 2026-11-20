import json
import os
from pathlib import Path

import pytest
import yaml

from boxxkite.handoff.adapters.continue_cli import SANDBOX_SECRET_CONFIG_PATH, ContinueAdapter
from boxxkite.handoff.core import HandoffError


SESSION_A = "12345678-1234-4234-8234-123456789abc"
SESSION_B = "f61fc91b-c679-4a12-9fb0-52f3a36025b3"
CONFIG = """\
name: local
version: 1.0.0
schema: v1
models:
  - name: autocomplete
    provider: openai
    model: small
    apiKey: sk-autocomplete
    roles: [autocomplete]
  - name: claude
    provider: anthropic
    model: claude-sonnet-4-5
    apiKey: sk-ant-literal
  - name: gpt
    provider: openai
    model: gpt-4o
    apiKey: ${{ secrets.OPENAI_API_KEY }}
    roles: [chat]
mcpServers:
  - name: github
    command: npx
    env:
      GITHUB_TOKEN: ghp_should_not_leave
"""


def _write_config(home: Path, text: str = CONFIG) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.yaml").write_text(text, encoding="utf-8")


def _write_session(home: Path, name: str) -> Path:
    session = home / "sessions" / f"{name}.json"
    session.parent.mkdir(parents=True, exist_ok=True)
    session.write_text('{"sessionId":"' + name + '","history":[]}', encoding="utf-8")
    return session


def _pushed_config(located) -> dict:
    return yaml.safe_load(located.files[1].local_path.read_text(encoding="utf-8"))


def test_locates_latest_session_and_pushes_only_first_chat_model(tmp_path: Path):
    _write_config(tmp_path)
    older = _write_session(tmp_path, SESSION_A)
    newer = _write_session(tmp_path, SESSION_B)
    _write_session(tmp_path, "sessions")
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))

    located = ContinueAdapter(continue_home=tmp_path).locate_session()

    assert located.tool == "continue"
    assert located.session_id == SESSION_B
    assert located.files[0].local_path == newer
    assert located.files[0].sandbox_path == f"/workspace/.continue/sessions/{SESSION_B}.json"
    assert located.files[1].sandbox_path == SANDBOX_SECRET_CONFIG_PATH
    assert SANDBOX_SECRET_CONFIG_PATH.startswith("/tmp/")
    assert located.resume_command == (
        f"cn --config {SANDBOX_SECRET_CONFIG_PATH} --resume; rm -f {SANDBOX_SECRET_CONFIG_PATH}"
    )
    assert located.workdir == "/workspace"
    assert located.credential is None
    assert _pushed_config(located) == {
        "name": "local",
        "version": "1.0.0",
        "schema": "v1",
        "models": [
            {"name": "claude", "provider": "anthropic", "model": "claude-sonnet-4-5", "apiKey": "sk-ant-literal"}
        ],
    }

    scoped = located.files[1].local_path
    located.cleanup()
    assert not scoped.exists()


def test_pushes_persisted_model_with_secret_filled_in(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-from-env")
    _write_config(tmp_path, CONFIG + "# apiKey: ${{ secrets.OLD_UNSET_KEY }}\n")
    (tmp_path / "index").mkdir()
    (tmp_path / "index" / "globalContext.json").write_text(json.dumps({"cliSelectedModel": "gpt"}))
    _write_session(tmp_path, SESSION_A)

    located = ContinueAdapter(continue_home=tmp_path).locate_session()

    [model] = _pushed_config(located)["models"]
    assert model["name"] == "gpt"
    assert model["apiKey"] == "sk-openai-from-env"


def test_matches_cn_model_choice_for_duplicates_and_nested_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("FOO", "token-from-env")
    _write_config(
        tmp_path,
        """\
name: local
version: 1.0.0
schema: v1
models:
  - name: codestral
    provider: mistral
    model: codestral
    roles: [autocomplete]
  - name: codestral
    provider: other
    model: shadowed
  - name: gpt
    provider: openai
    model: gpt-4o
    requestOptions:
      headers:
        Authorization: Bearer ${{ secrets.org/pkg/FOO }}
""",
    )
    (tmp_path / "index").mkdir()
    (tmp_path / "index" / "globalContext.json").write_text(json.dumps({"cliSelectedModel": "codestral"}))
    _write_session(tmp_path, SESSION_A)

    [model] = _pushed_config(ContinueAdapter(continue_home=tmp_path).locate_session())["models"]

    assert model["name"] == "gpt"
    assert model["requestOptions"]["headers"]["Authorization"] == "Bearer token-from-env"


def test_rejects_unset_secret_on_active_model(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    _write_config(tmp_path)
    (tmp_path / "index").mkdir()
    (tmp_path / "index" / "globalContext.json").write_text(json.dumps({"cliSelectedModel": "gpt"}))
    _write_session(tmp_path, SESSION_A)

    with pytest.raises(HandoffError, match="OPENAI_API_KEY is not set"):
        ContinueAdapter(continue_home=tmp_path).locate_session()


def test_rejects_uses_blocks_in_any_yaml_style(tmp_path: Path):
    _write_config(tmp_path, "name: local\nversion: 1.0.0\nschema: v1\nmodels: [{uses: anthropic/claude-sonnet}]\n")
    _write_session(tmp_path, SESSION_A)

    with pytest.raises(HandoffError, match="uses:"):
        ContinueAdapter(continue_home=tmp_path).locate_session()


def test_rejects_non_secret_template(tmp_path: Path):
    _write_config(tmp_path, CONFIG.replace("sk-ant-literal", "${{ inputs.KEY }}"))
    _write_session(tmp_path, SESSION_A)

    with pytest.raises(HandoffError, match="cannot resolve"):
        ContinueAdapter(continue_home=tmp_path).locate_session()


def test_requires_local_config(tmp_path: Path):
    _write_session(tmp_path, SESSION_A)

    with pytest.raises(HandoffError, match="No local Continue config"):
        ContinueAdapter(continue_home=tmp_path).locate_session()


def test_ignores_non_uuid_session_file(tmp_path: Path):
    _write_config(tmp_path)
    _write_session(tmp_path, "x; echo injected")

    with pytest.raises(HandoffError, match="No resumable"):
        ContinueAdapter(continue_home=tmp_path).locate_session()


def test_selects_explicit_session_and_rejects_bad_refs(tmp_path: Path):
    _write_config(tmp_path)
    older = _write_session(tmp_path, SESSION_A)
    newer = _write_session(tmp_path, SESSION_B)
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    adapter = ContinueAdapter(continue_home=tmp_path)

    assert adapter.locate_session(session_ref=SESSION_A.upper()).files[0].local_path == older
    with pytest.raises(HandoffError, match="not a valid UUID"):
        adapter.locate_session(session_ref="latest")
    with pytest.raises(HandoffError, match="No local Continue CLI session found"):
        adapter.locate_session(session_ref="00000000-0000-4000-8000-000000000000")


def test_honors_continue_global_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CONTINUE_GLOBAL_DIR", str(tmp_path))
    _write_config(tmp_path)
    session = _write_session(tmp_path, SESSION_A)

    located = ContinueAdapter().locate_session()

    assert located.files[0].local_path == session
