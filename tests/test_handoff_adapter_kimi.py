from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from boxxkite.handoff.adapters.kimi import KimiAdapter
from boxxkite.handoff.core import HandoffError


def _write_session(
    kimi_home: Path,
    *,
    bucket: str,
    session_id: str,
    cwd: str = "/Users/dev/some-project",
    agent_id: str = "main",
) -> Path:
    """Real kimi-code sessions live at
    sessions/<bucket>/<session_id>/{state.json,agents/<id>/wire.jsonl,...} --
    this reproduces that shape, not a flat file."""
    session_dir = kimi_home / "sessions" / bucket / session_id
    agent_dir = session_dir / "agents" / agent_id
    agent_dir.mkdir(parents=True)
    (session_dir / "logs").mkdir()
    (agent_dir / "wire.jsonl").write_text('{"type":"assistant","content":"hi"}\n', encoding="utf-8")
    (session_dir / "logs" / "kimi-code.log").write_text("log line\n", encoding="utf-8")
    state = {
        "id": session_id,
        "version": 2,
        "cwd": cwd,
        "createdAt": 1786354717617,
        "updatedAt": 1786354737225,
        "archived": False,
        "agents": {agent_id: {"homedir": str(agent_dir), "type": agent_id}},
        "custom": {},
    }
    (session_dir / "state.json").write_text(json.dumps(state), encoding="utf-8")
    return session_dir


def _write_config(kimi_home: Path, *, provider: str = "moonshotai", api_key: str = "test-key-123") -> None:
    kimi_home.mkdir(parents=True, exist_ok=True)
    config = kimi_home / "config.toml"
    config.write_text(
        f"""default_model = "{provider}/kimi-k2-turbo-preview"

[providers.{provider}]
base_url = "https://api.moonshot.ai/v1"
type = "openai"
api_key = "{api_key}"

[models."{provider}/kimi-k2-turbo-preview"]
provider = "{provider}"
model = "kimi-k2-turbo-preview"

[models."{provider}/kimi-k3"]
provider = "{provider}"
model = "kimi-k3"

[models."other-provider/some-model"]
provider = "other-provider"
model = "some-model"
""",
        encoding="utf-8",
    )


def _real_bucket_name(directory: str) -> str:
    """Independently reproduces the real kimi-code bucket-key algorithm
    (verified byte-for-byte against a real bucket directory kimi itself
    created on disk), so tests assert against ground truth rather than
    whatever the adapter happens to compute."""
    resolved = str(Path(directory).resolve())
    digest = hashlib.sha256(resolved.encode()).hexdigest()[:12]
    return f"wd_{Path(resolved).name}_{digest}"


def test_locate_session_finds_nested_bucket_session(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    _write_config(kimi_home)
    _write_session(kimi_home, bucket="wd_myproject_abc123", session_id="session_uuid-1")

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session()

    assert located.tool == "kimi"
    assert located.session_id == "session_uuid-1"
    assert located.resume_command == "kimi --session session_uuid-1"
    assert located.workdir == "/workspace"
    assert located.credential is None


def test_locate_session_pushes_every_real_file_in_the_session_directory(tmp_path: Path):
    """A kimi session is a directory tree, not a single file -- every real
    file under it (state.json, agents/*/wire.jsonl, logs/*) must be pushed,
    each preserving its path relative to the session root."""
    kimi_home = tmp_path / ".kimi-code"
    _write_config(kimi_home)
    _write_session(kimi_home, bucket="wd_myproject_abc123", session_id="session_uuid-1")

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session()

    sandbox_paths = {f.sandbox_path for f in located.files}
    expected_bucket = _real_bucket_name("/workspace")
    base = f"/workspace/.kimi-code/sessions/{expected_bucket}/session_uuid-1"
    assert f"{base}/agents/main/wire.jsonl" in sandbox_paths
    assert f"{base}/logs/kimi-code.log" in sandbox_paths
    assert f"{base}/state.json" in sandbox_paths
    # Plus the synthesized index and scoped config, not part of the session dir.
    assert "/workspace/.kimi-code/session_index.jsonl" in sandbox_paths
    assert "/workspace/.kimi-code/config.toml" in sandbox_paths


def test_sandbox_path_uses_the_real_bucket_hash_algorithm(tmp_path: Path):
    """Confirmed against a real bucket directory kimi-code itself created:
    wd_<basename>_<first 12 hex chars of sha256(realpath)>."""
    kimi_home = tmp_path / ".kimi-code"
    _write_config(kimi_home)
    _write_session(kimi_home, bucket="wd_myproject_abc123", session_id="session_uuid-1")

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session()

    expected_bucket = _real_bucket_name("/workspace")
    assert expected_bucket.startswith("wd_workspace_")
    assert any(expected_bucket in f.sandbox_path for f in located.files)


def test_state_json_is_rewritten_to_the_sandbox_workdir(tmp_path: Path):
    """state.json's own cwd (and each agent's absolute homedir) are baked
    in at creation time and checked on resume -- kimi refuses to resume
    ("was created under a different directory") if cwd doesn't match
    exactly, verified live against the real CLI."""
    kimi_home = tmp_path / ".kimi-code"
    _write_config(kimi_home)
    _write_session(kimi_home, bucket="wd_myproject_abc123", session_id="session_uuid-1", cwd="/Users/dev/some-project")

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session()

    state_file = next(f for f in located.files if f.sandbox_path.endswith("/state.json"))
    rewritten = json.loads(state_file.local_path.read_text(encoding="utf-8"))
    assert rewritten["cwd"] == "/workspace"
    expected_bucket = _real_bucket_name("/workspace")
    expected_session_dir = f"/workspace/.kimi-code/sessions/{expected_bucket}/session_uuid-1"
    assert rewritten["agents"]["main"]["homedir"] == f"{expected_session_dir}/agents/main"


def test_session_index_entry_matches_sandbox_paths(tmp_path: Path):
    """kimi --session <id> looks the id up exclusively in
    session_index.jsonl (home-dir level, id -> absolute sessionDir/workDir)
    -- confirmed live there is no directory-scan fallback. A fresh sandbox
    has never written this file, so this adapter has to synthesize it."""
    kimi_home = tmp_path / ".kimi-code"
    _write_config(kimi_home)
    _write_session(kimi_home, bucket="wd_myproject_abc123", session_id="session_uuid-1")

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session()

    index_file = next(f for f in located.files if f.sandbox_path.endswith("session_index.jsonl"))
    record = json.loads(index_file.local_path.read_text(encoding="utf-8").strip())
    expected_bucket = _real_bucket_name("/workspace")
    expected_session_dir = f"/workspace/.kimi-code/sessions/{expected_bucket}/session_uuid-1"
    assert record == {
        "sessionId": "session_uuid-1",
        "sessionDir": expected_session_dir,
        "workDir": "/workspace",
    }


def test_scoped_config_includes_only_the_default_models_provider(tmp_path: Path):
    """Pushes just default_model's own provider block and that provider's
    model definitions, not the user's whole config.toml (which may hold
    other providers' keys uninvolved in this session)."""
    kimi_home = tmp_path / ".kimi-code"
    _write_config(kimi_home, provider="moonshotai")
    _write_session(kimi_home, bucket="wd_myproject_abc123", session_id="session_uuid-1")

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session()

    config_file = next(f for f in located.files if f.sandbox_path.endswith("config.toml"))
    content = config_file.local_path.read_text(encoding="utf-8")
    assert 'default_model = "moonshotai/kimi-k2-turbo-preview"' in content
    assert "moonshotai" in content
    assert "test-key-123" in content
    assert "other-provider" not in content


def test_credential_is_none_config_pushed_as_a_plain_file_instead(tmp_path: Path):
    """Real Kimi Code CLI has no env-var auth path at all (verified live:
    KIMI_API_KEY set with no config.toml still fails with "No model
    configured") -- the only real credential source is config.toml itself,
    so this adapter pushes it as one more file rather than using the
    Credential/env-var export mechanism."""
    kimi_home = tmp_path / ".kimi-code"
    _write_config(kimi_home)
    _write_session(kimi_home, bucket="wd_myproject_abc123", session_id="session_uuid-1")

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session()

    assert located.credential is None
    assert any(f.sandbox_path.endswith("config.toml") for f in located.files)


def test_locate_session_by_explicit_id_across_multiple_buckets(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    _write_config(kimi_home)
    _write_session(kimi_home, bucket="wd_bucket-a_111111", session_id="session_a")
    _write_session(kimi_home, bucket="wd_bucket-b_222222", session_id="session_b")

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session(session_ref="session_b")

    assert located.session_id == "session_b"


def test_locate_session_raises_for_unknown_session_ref(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    _write_config(kimi_home)
    _write_session(kimi_home, bucket="wd_bucket-a_111111", session_id="session_a")

    adapter = KimiAdapter(kimi_home=kimi_home)
    with pytest.raises(HandoffError, match="No local Kimi session found"):
        adapter.locate_session(session_ref="does-not-exist")


def test_locate_session_picks_most_recently_modified_across_buckets(tmp_path: Path):
    import os
    import time

    kimi_home = tmp_path / ".kimi-code"
    _write_config(kimi_home)
    older = _write_session(kimi_home, bucket="wd_bucket-a_111111", session_id="session_old")
    newer = _write_session(kimi_home, bucket="wd_bucket-b_222222", session_id="session_new")
    now = time.time()
    os.utime(older, (now - 1000, now - 1000))
    os.utime(newer, (now, now))

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session()

    assert located.session_id == "session_new"


def test_missing_sessions_directory_raises(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    _write_config(kimi_home)

    adapter = KimiAdapter(kimi_home=kimi_home)
    with pytest.raises(HandoffError, match="No Kimi sessions directory found"):
        adapter.locate_session()


def test_bucket_dir_with_no_valid_session_raises(tmp_path: Path):
    """A directory under sessions/<bucket>/ with no state.json isn't a real
    session (could be leftover/corrupt) -- must not be treated as one."""
    kimi_home = tmp_path / ".kimi-code"
    _write_config(kimi_home)
    (kimi_home / "sessions" / "wd_bucket_abc" / "not-a-real-session").mkdir(parents=True)

    adapter = KimiAdapter(kimi_home=kimi_home)
    with pytest.raises(HandoffError, match="No local Kimi sessions found"):
        adapter.locate_session()


def test_missing_config_raises(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    _write_session(kimi_home, bucket="wd_bucket_abc", session_id="session_1")

    adapter = KimiAdapter(kimi_home=kimi_home)
    with pytest.raises(HandoffError, match="No Kimi config found"):
        adapter.locate_session()


def test_config_missing_default_model_raises(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    kimi_home.mkdir(parents=True)
    (kimi_home / "config.toml").write_text('[providers.moonshotai]\napi_key = "x"\n', encoding="utf-8")
    _write_session(kimi_home, bucket="wd_bucket_abc", session_id="session_1")

    adapter = KimiAdapter(kimi_home=kimi_home)
    with pytest.raises(HandoffError, match="No usable default_model"):
        adapter.locate_session()


def test_config_default_model_provider_missing_raises(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    kimi_home.mkdir(parents=True)
    (kimi_home / "config.toml").write_text('default_model = "ghost-provider/some-model"\n', encoding="utf-8")
    _write_session(kimi_home, bucket="wd_bucket_abc", session_id="session_1")

    adapter = KimiAdapter(kimi_home=kimi_home)
    with pytest.raises(HandoffError, match="ghost-provider"):
        adapter.locate_session()


def test_cleanup_removes_every_temp_file(tmp_path: Path):
    kimi_home = tmp_path / ".kimi-code"
    _write_config(kimi_home)
    _write_session(kimi_home, bucket="wd_myproject_abc123", session_id="session_uuid-1")

    adapter = KimiAdapter(kimi_home=kimi_home)
    located = adapter.locate_session()

    temp_paths = [f.local_path for f in located.files if f.local_path.name in ("state.json", "session_index.jsonl", "config.toml") and "boxxkite-handoff-kimi" in str(f.local_path)]
    assert temp_paths, "expected at least one synthesized temp file"
    assert located.cleanup is not None
    located.cleanup()
    for p in temp_paths:
        assert not p.exists()
