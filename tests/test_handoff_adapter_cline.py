import json
import os
from pathlib import Path

import pytest

from boxxkite.handoff.adapters.cline import MARKER_NAME, SANDBOX_DATA_DIR, SANDBOX_PROVIDERS_PATH, ClineAdapter
from boxxkite.handoff.core import HandoffError


SESSION_A = "1794812700000_aaaaa"
SESSION_B = "1794812731019_hkgtv"
PROVIDERS = {
    "version": 1,
    "lastUsedProvider": "anthropic",
    "modes": {},
    "providers": {
        "openai-compatible": {
            "settings": {"provider": "openai-compatible", "apiKey": "sk-openai", "baseUrl": "http://x/v1"},
            "tokenSource": "manual",
        },
        "anthropic": {"settings": {"provider": "anthropic", "apiKey": "sk-ant-other"}, "tokenSource": "manual"},
        "cline": {"settings": {"provider": "cline"}, "tokenSource": "oauth"},
    },
}


def _write_providers(data_dir: Path, providers: dict = PROVIDERS) -> None:
    settings = data_dir / "settings"
    settings.mkdir(parents=True, exist_ok=True)
    (settings / "providers.json").write_text(json.dumps(providers), encoding="utf-8")


def _write_session(data_dir: Path, session_id: str, provider: str = "openai-compatible") -> tuple[Path, Path]:
    session_dir = data_dir / "sessions" / session_id
    session_dir.mkdir(parents=True)
    meta = session_dir / f"{session_id}.json"
    meta.write_text(
        json.dumps(
            {
                "session_id": session_id,
                "provider": provider,
                "cwd": "/original/project",
                "messages_path": str(session_dir / f"{session_id}.messages.json"),
            }
        ),
        encoding="utf-8",
    )
    messages = session_dir / f"{session_id}.messages.json"
    messages.write_text('{"messages":[]}', encoding="utf-8")
    return meta, messages


def _read(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_locates_latest_session_and_stages_it_under_tmp(tmp_path: Path):
    _write_providers(tmp_path)
    old_meta, _ = _write_session(tmp_path, SESSION_A)
    new_meta, new_messages = _write_session(tmp_path, SESSION_B)
    os.utime(old_meta, (1, 1))
    os.utime(new_meta, (2, 2))

    located = ClineAdapter(data_dir=tmp_path).locate_session()

    assert located.tool == "cline"
    assert located.session_id == SESSION_B
    root_marker, sessions_marker, meta_file, messages_file, providers_file = located.files
    assert root_marker.sandbox_path == f"{SANDBOX_DATA_DIR}/{MARKER_NAME}"
    assert sessions_marker.sandbox_path == f"{SANDBOX_DATA_DIR}/sessions/{MARKER_NAME}"
    sandbox_session_dir = f"{SANDBOX_DATA_DIR}/sessions/{SESSION_B}"
    assert SANDBOX_DATA_DIR.startswith("/tmp/")
    assert meta_file.sandbox_path == f"{sandbox_session_dir}/{SESSION_B}.json"
    assert _read(meta_file.local_path)["messages_path"] == f"{sandbox_session_dir}/{SESSION_B}.messages.json"
    assert _read(new_meta)["messages_path"].startswith(str(tmp_path))
    assert (messages_file.local_path, messages_file.sandbox_path) == (
        new_messages,
        f"{sandbox_session_dir}/{SESSION_B}.messages.json",
    )
    assert providers_file.sandbox_path == SANDBOX_PROVIDERS_PATH
    assert _read(providers_file.local_path) == {
        "version": 1,
        "lastUsedProvider": "openai-compatible",
        "providers": {"openai-compatible": PROVIDERS["providers"]["openai-compatible"]},
    }
    assert located.resume_command == (
        f"cline --data-dir {SANDBOX_DATA_DIR} --id {SESSION_B}; rm -f {SANDBOX_PROVIDERS_PATH}"
    )
    assert located.workdir == "/workspace"
    assert located.credential is None

    staged = meta_file.local_path
    located.cleanup()
    assert not staged.exists()


def test_selects_explicit_session(tmp_path: Path):
    _write_providers(tmp_path)
    old_meta, _ = _write_session(tmp_path, SESSION_A, provider="anthropic")
    new_meta, _ = _write_session(tmp_path, SESSION_B)
    os.utime(old_meta, (1, 1))
    os.utime(new_meta, (2, 2))

    located = ClineAdapter(data_dir=tmp_path).locate_session(session_ref=SESSION_A)

    assert located.session_id == SESSION_A
    assert _read(located.files[-1].local_path)["lastUsedProvider"] == "anthropic"


@pytest.mark.parametrize(
    "entry",
    [
        {"settings": {"provider": "cline"}, "tokenSource": "oauth"},
        {"settings": {"provider": "cline", "auth": {"accessToken": "a", "refreshToken": "r"}}, "tokenSource": "migration"},
    ],
)
def test_rejects_account_login_provider(tmp_path: Path, entry: dict):
    _write_providers(tmp_path, {**PROVIDERS, "providers": {"cline": entry}})
    _write_session(tmp_path, SESSION_A, provider="cline")

    with pytest.raises(HandoffError, match="account login"):
        ClineAdapter(data_dir=tmp_path).locate_session()


@pytest.mark.parametrize("token_source", [None, "migration"])
def test_accepts_api_key_entries_without_manual_token_source(tmp_path: Path, token_source):
    entry = {"settings": {"provider": "gemini", "apiKey": "key"}}
    if token_source:
        entry["tokenSource"] = token_source
    _write_providers(tmp_path, {"version": 1, "providers": {"gemini": entry}})
    _write_session(tmp_path, SESSION_A, provider="gemini")

    located = ClineAdapter(data_dir=tmp_path).locate_session()

    assert _read(located.files[-1].local_path)["providers"] == {"gemini": entry}


def test_carries_compaction_state_and_drops_stale_path(tmp_path: Path):
    _write_providers(tmp_path)
    meta, _ = _write_session(tmp_path, SESSION_A)
    compaction = meta.with_name(f"{SESSION_A}.compaction.json")
    compaction.write_text("{}", encoding="utf-8")
    sandbox_compaction = f"{SANDBOX_DATA_DIR}/sessions/{SESSION_A}/{SESSION_A}.compaction.json"

    located = ClineAdapter(data_dir=tmp_path).locate_session()
    pushed = {f.sandbox_path: f.local_path for f in located.files}
    assert pushed[sandbox_compaction] == compaction
    assert _read(located.files[2].local_path)["compaction_path"] == sandbox_compaction

    compaction.unlink()
    meta.write_text(json.dumps({**_read(meta), "compaction_path": "/Users/me/old.json"}), encoding="utf-8")
    located = ClineAdapter(data_dir=tmp_path).locate_session()
    assert "compaction_path" not in _read(located.files[2].local_path)


def test_rejects_malformed_provider_settings(tmp_path: Path):
    _write_providers(tmp_path, {"version": 1, "providers": []})
    _write_session(tmp_path, SESSION_A)

    with pytest.raises(HandoffError, match="no settings"):
        ClineAdapter(data_dir=tmp_path).locate_session()


def test_rejects_session_provider_missing_from_settings(tmp_path: Path):
    _write_providers(tmp_path)
    _write_session(tmp_path, SESSION_A, provider="gemini")

    with pytest.raises(HandoffError, match="no settings for the session's provider"):
        ClineAdapter(data_dir=tmp_path).locate_session()


def test_requires_provider_settings(tmp_path: Path):
    _write_session(tmp_path, SESSION_A)

    with pytest.raises(HandoffError, match="No Cline provider settings"):
        ClineAdapter(data_dir=tmp_path).locate_session()


def test_ignores_session_without_messages_file(tmp_path: Path):
    _write_providers(tmp_path)
    _, messages = _write_session(tmp_path, SESSION_A)
    messages.unlink()

    with pytest.raises(HandoffError, match="No resumable"):
        ClineAdapter(data_dir=tmp_path).locate_session()


def test_rejects_unsafe_and_unknown_session_refs(tmp_path: Path):
    _write_providers(tmp_path)
    _write_session(tmp_path, SESSION_A)
    adapter = ClineAdapter(data_dir=tmp_path)

    with pytest.raises(HandoffError, match="outside"):
        adapter.locate_session(session_ref="x; echo injected")
    with pytest.raises(HandoffError, match="No local Cline session found"):
        adapter.locate_session(session_ref=SESSION_B)


def test_rejects_unsafe_session_directory_name(tmp_path: Path):
    _write_providers(tmp_path)
    _write_session(tmp_path, "x; echo injected")

    with pytest.raises(HandoffError, match="outside"):
        ClineAdapter(data_dir=tmp_path).locate_session()
