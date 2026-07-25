"""Tests for the `boxkite` CLI (src/boxkite/cli/). All HTTP calls are
mocked at the httpx level — no real control-plane or sidecar is required.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from boxkite.cli import app
from boxkite.cli import client as client_module
from boxkite.cli import config_store
from boxkite.cli.context import Context
from boxkite.cli.errors import CliError

runner = CliRunner()

# Whole-word match so legitimate compound words (e.g. "control-plane") don't
# trip the check — only look for these as standalone pricing/plan-tier terms.
# Negative lookbehind excludes "trust-tier"/"trust_tier" specifically: that's
# `secrets create`'s real --trust-tier flag (docs/WALLET-SECRETS-DESIGN.md),
# a wallet/private-key security classification with nothing to do with
# pricing -- a legitimate, unrelated use of the word "tier", not an exception
# that weakens the actual no-pricing-language rule.
BANNED_WORD_PATTERN = re.compile(
    r"\b(dollar|price|pricing|plan|subscription|billing)\b|(?<!trust-)(?<!trust_)\btier\b",
    re.IGNORECASE,
)
# Rich (typer's help-output renderer) emits ANSI escape codes even under
# CliRunner's non-tty invocation, and it can interleave them between
# characters of a single flag name (e.g. "-trust\x1b[0m\x1b[1;36m-tier") --
# stripped before matching so BANNED_WORD_PATTERN's own lookbehind
# exceptions (and the check in general) see the real adjacent text, not
# color-code noise sitting in between.
_ANSI_ESCAPE_PATTERN = re.compile(r"\x1b\[[0-9;]*m")


class FakeResponse:
    def __init__(self, status_code: int, json_data=None, has_content: bool = True):
        self.status_code = status_code
        self._json = json_data
        self.content = b"x" if has_content else b""

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """Point config_store's file paths at a scratch directory so no test
    reads or writes the real ~/.boxkite."""
    config_dir = tmp_path / ".boxkite"
    monkeypatch.setattr(config_store, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(config_store, "LOCAL_ENV_FILE", config_dir / "local.env")
    yield


# ── Config file read/write round-trips ──────────────────────────────────
def test_hosted_config_roundtrip():
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_abc")

    result = config_store.read_hosted_config()

    assert result.base_url == "https://cp.example.com"
    assert result.api_key == "bxk_live_abc"


def test_hosted_config_partial_writes_preserve_existing_fields():
    config_store.write_hosted_config(base_url="https://cp.example.com")
    config_store.write_hosted_config(api_key="bxk_live_abc")

    result = config_store.read_hosted_config()

    assert result.base_url == "https://cp.example.com"
    assert result.api_key == "bxk_live_abc"


def test_hosted_config_missing_file_returns_empty():
    result = config_store.read_hosted_config()

    assert result.base_url is None
    assert result.api_key is None


def test_local_env_roundtrip():
    config_store.write_local_env(token="tok123", sidecar_url="http://localhost:8080")

    result = config_store.read_local_env()

    assert result is not None
    assert result.token == "tok123"
    assert result.sidecar_url == "http://localhost:8080"


def test_local_env_missing_file_returns_none():
    assert config_store.read_local_env() is None


# ── boxkite config CLI commands ──────────────────────────────────────────
def test_config_set_key_and_show_masks_value():
    set_result = runner.invoke(app, ["config", "set-key", "bxk_live_1234567890abcdef"])
    assert set_result.exit_code == 0

    show_result = runner.invoke(app, ["config", "show"])
    assert show_result.exit_code == 0
    assert "bxk_live_1234567890abcdef" not in show_result.output
    assert "bxk_live_1" in show_result.output  # masked prefix is shown


def test_config_set_url_strips_trailing_slash():
    result = runner.invoke(app, ["config", "set-url", "https://cp.example.com/"])
    assert result.exit_code == 0

    cfg = config_store.read_hosted_config()
    assert cfg.base_url == "https://cp.example.com"


def test_config_set_url_rejects_plain_http_to_a_remote_host():
    """A bxk_live_... API key is a full-privilege, long-lived credential --
    sent as `Authorization: Bearer` on every hosted request
    (src/boxkite/cli/client.py's hosted_request). An http:// URL to
    anything other than localhost would put it on the wire in cleartext."""
    result = runner.invoke(app, ["config", "set-url", "http://cp.example.com"])

    assert result.exit_code == 1
    assert "cleartext" in result.output

    cfg = config_store.read_hosted_config()
    assert cfg.base_url is None  # never persisted


def test_config_set_url_allows_http_localhost_for_local_dev():
    result = runner.invoke(app, ["config", "set-url", "http://localhost:8090"])
    assert result.exit_code == 0

    cfg = config_store.read_hosted_config()
    assert cfg.base_url == "http://localhost:8090"


def test_signup_rejects_plain_http_before_sending_any_request(monkeypatch):
    """The scheme must be validated BEFORE the signup/api-key requests --
    not just at the final config-save step -- otherwise the freshly-issued
    JWT and API key would already have been sent in cleartext by the time a
    later check ran."""

    def _boom(*args, **kwargs):
        raise AssertionError("must not send any request for a rejected base_url")

    monkeypatch.setattr("httpx.post", _boom)

    result = runner.invoke(
        app,
        [
            "signup",
            "--email",
            "test@example.com",
            "--password",
            "correcthorse123",
            "--url",
            "http://cp.example.com",
        ],
    )

    assert result.exit_code == 1
    assert "cleartext" in result.output


# ── resolve_session_id: zero / one / many active sessions ───────────────
def _hosted_ctx() -> Context:
    return Context(mode="hosted", base_url="https://cp.example.com", api_key="bxk_live_x")


def test_resolve_session_id_uses_explicit_session_without_calling_api(monkeypatch):
    def _boom(*args, **kwargs):
        raise AssertionError("should not call httpx when --session is explicit")

    monkeypatch.setattr(client_module.httpx, "request", _boom)

    session_id = client_module.resolve_session_id(_hosted_ctx(), "explicit-id")

    assert session_id == "explicit-id"


def test_resolve_session_id_auto_picks_single_active_session(monkeypatch):
    monkeypatch.setattr(
        client_module.httpx,
        "request",
        lambda *a, **k: FakeResponse(200, json_data=[{"id": "sess-1", "status": "active"}]),
    )

    session_id = client_module.resolve_session_id(_hosted_ctx(), None)

    assert session_id == "sess-1"


def test_resolve_session_id_raises_on_zero_active_sessions(monkeypatch):
    monkeypatch.setattr(client_module.httpx, "request", lambda *a, **k: FakeResponse(200, json_data=[]))

    with pytest.raises(CliError, match="No active sandbox sessions"):
        client_module.resolve_session_id(_hosted_ctx(), None)


def test_resolve_session_id_raises_on_multiple_active_sessions(monkeypatch):
    monkeypatch.setattr(
        client_module.httpx,
        "request",
        lambda *a, **k: FakeResponse(
            200, json_data=[{"id": "sess-1", "status": "active"}, {"id": "sess-2", "status": "active"}]
        ),
    )

    with pytest.raises(CliError, match="Multiple active sandbox sessions"):
        client_module.resolve_session_id(_hosted_ctx(), None)


# ── exec command: hosted auto-select and local mode ──────────────────────
def test_exec_hosted_auto_selects_single_session(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")
    calls = []

    def fake_request(method, url, **kwargs):
        calls.append((method, url))
        if url.endswith("/v1/sandboxes") and method == "GET":
            return FakeResponse(200, json_data=[{"id": "sess-1", "status": "active"}])
        if url.endswith("/v1/sandboxes/sess-1/exec"):
            return FakeResponse(200, json_data={"exit_code": 0, "stdout": "hi\n", "stderr": ""})
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(app, ["exec", "echo hi"])

    assert result.exit_code == 0
    assert "exit code: 0" in result.output
    assert any(url.endswith("/exec") for _, url in calls)


def test_exec_hosted_fails_clearly_with_zero_sessions(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")
    monkeypatch.setattr(client_module.httpx, "request", lambda *a, **k: FakeResponse(200, json_data=[]))

    result = runner.invoke(app, ["exec", "echo hi"])

    assert result.exit_code == 1
    assert "No active sandbox sessions" in result.output


def test_exec_hosted_fails_clearly_with_multiple_sessions(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")
    monkeypatch.setattr(
        client_module.httpx,
        "request",
        lambda *a, **k: FakeResponse(
            200, json_data=[{"id": "sess-1", "status": "active"}, {"id": "sess-2", "status": "active"}]
        ),
    )

    result = runner.invoke(app, ["exec", "echo hi"])

    assert result.exit_code == 1
    assert "Multiple active sandbox sessions" in result.output
    assert "--session" in result.output


def test_exec_hosted_respects_explicit_session_over_auto_select(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")

    def fake_request(method, url, **kwargs):
        if url.endswith("/v1/sandboxes"):
            raise AssertionError("should not list sessions when --session is given")
        if url.endswith("/v1/sandboxes/explicit-id/exec"):
            return FakeResponse(200, json_data={"exit_code": 0, "stdout": "ok\n", "stderr": ""})
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(app, ["exec", "echo hi", "--session", "explicit-id"])

    assert result.exit_code == 0


def test_exec_local_mode_calls_sidecar_directly(monkeypatch):
    config_store.write_local_env(token="tok123", sidecar_url="http://localhost:8080")

    def fake_request(method, url, **kwargs):
        assert url == "http://localhost:8080/exec"
        assert kwargs["headers"]["X-Sidecar-Auth-Token"] == "tok123"
        return FakeResponse(200, json_data={"exit_code": 0, "stdout": "local ok\n", "stderr": ""})

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(app, ["exec", "echo hi"])

    assert result.exit_code == 0
    assert "local ok" in result.output


def test_exec_with_no_target_configured_fails_clearly():
    result = runner.invoke(app, ["exec", "echo hi"])

    assert result.exit_code == 1
    assert "No boxkite target configured" in result.output


# ── session command: hosted-only, honest local-mode error ────────────────
def test_session_ls_in_local_mode_explains_capability_gap():
    config_store.write_local_env(token="tok123", sidecar_url="http://localhost:8080")

    result = runner.invoke(app, ["session", "ls"])

    assert result.exit_code == 1
    assert "no session-management" in result.output.lower() or "no session" in result.output.lower()


def test_session_ls_lists_multiple_sessions(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")
    sessions = [
        {"id": "sess-1", "status": "active", "label": None, "created_at": "2026-01-01T00:00:00Z"},
        {"id": "sess-2", "status": "destroyed", "label": "nightly", "created_at": "2026-01-02T00:00:00Z"},
    ]
    monkeypatch.setattr(client_module.httpx, "request", lambda *a, **k: FakeResponse(200, json_data=sessions))

    result = runner.invoke(app, ["session", "ls"])

    assert result.exit_code == 0
    assert "sess-1" in result.output
    assert "sess-2" in result.output


def test_session_get_prints_single_session(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")
    session = {"id": "sess-1", "status": "active", "label": "demo", "created_at": "2026-01-01T00:00:00Z"}
    monkeypatch.setattr(client_module.httpx, "request", lambda *a, **k: FakeResponse(200, json_data=session))

    result = runner.invoke(app, ["session", "get", "sess-1"])

    assert result.exit_code == 0
    assert "sess-1" in result.output
    assert "demo" in result.output


def test_session_create_handles_batch_response_as_bare_list(monkeypatch):
    # count>1 makes control-plane return a bare list, not {"sandboxes": [...]}
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")
    created = [
        {"id": "sess-1", "status": "active"},
        {"id": "sess-2", "status": "active"},
    ]
    monkeypatch.setattr(client_module.httpx, "request", lambda *a, **k: FakeResponse(201, json_data=created))

    result = runner.invoke(app, ["session", "create", "--count", "2"])

    assert result.exit_code == 0
    assert "sess-1" in result.output
    assert "sess-2" in result.output


def test_session_create_handles_single_object_response(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")
    created = {"id": "sess-1", "status": "active"}
    monkeypatch.setattr(client_module.httpx, "request", lambda *a, **k: FakeResponse(201, json_data=created))

    result = runner.invoke(app, ["session", "create"])

    assert result.exit_code == 0
    assert "sess-1" in result.output


def test_session_create_passes_image_id_secret_names_and_volume_mounts(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")
    created = {"id": "sess-1", "status": "active"}
    captured_bodies = []

    def _fake_request(*args, **kwargs):
        captured_bodies.append(kwargs.get("json"))
        return FakeResponse(201, json_data=created)

    monkeypatch.setattr(client_module.httpx, "request", _fake_request)

    result = runner.invoke(
        app,
        [
            "session",
            "create",
            "--image-id",
            "img-1",
            "--secret-names",
            "foo",
            "--secret-names",
            "bar",
            "--volume-mounts",
            "vol-1=/mnt/data",
            "--volume-mounts",
            "vol-2=/mnt/cache",
            "--gpu-count",
            "2",
        ],
    )

    assert result.exit_code == 0
    assert "sess-1" in result.output
    assert len(captured_bodies) == 1
    body = captured_bodies[0]
    assert body["image_id"] == "img-1"
    assert body["secret_names"] == ["foo", "bar"]
    assert body["volume_mounts"] == {"vol-1": "/mnt/data", "vol-2": "/mnt/cache"}
    assert body["gpu_count"] == 2


def test_session_create_omits_image_secrets_volumes_when_not_passed(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")
    created = {"id": "sess-1", "status": "active"}
    captured_bodies = []

    def _fake_request(*args, **kwargs):
        captured_bodies.append(kwargs.get("json"))
        return FakeResponse(201, json_data=created)

    monkeypatch.setattr(client_module.httpx, "request", _fake_request)

    result = runner.invoke(app, ["session", "create"])

    assert result.exit_code == 0
    body = captured_bodies[0]
    assert "image_id" not in body
    assert "secret_names" not in body
    assert "gpu_count" not in body
    assert "volume_mounts" not in body


def test_session_create_rejects_malformed_volume_mounts(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")

    def _fail_request(*args, **kwargs):
        raise AssertionError("should not call httpx with a malformed --volume-mounts value")

    monkeypatch.setattr(client_module.httpx, "request", _fail_request)

    result = runner.invoke(app, ["session", "create", "--volume-mounts", "no-equals-sign"])

    assert result.exit_code == 1
    assert "Invalid --volume-mounts value" in result.output


# ── files command: ls/glob/grep ───────────────────────────────────────────
def test_files_ls_lists_entries(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")
    monkeypatch.setattr(
        client_module.httpx,
        "request",
        lambda *a, **k: FakeResponse(
            200, json_data={"entries": [{"path": "/workspace/a.py", "is_dir": False}, {"path": "/workspace/sub", "is_dir": True}]}
        ),
    )

    result = runner.invoke(app, ["files", "ls", "/workspace", "--session", "sess-1"])

    assert result.exit_code == 0
    assert "/workspace/a.py" in result.output
    assert "/workspace/sub/" in result.output


def test_files_glob_lists_matches(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")
    monkeypatch.setattr(
        client_module.httpx,
        "request",
        lambda *a, **k: FakeResponse(200, json_data={"matches": [{"path": "/workspace/a.py", "is_dir": False}]}),
    )

    result = runner.invoke(app, ["files", "glob", "**/*.py", "--session", "sess-1"])

    assert result.exit_code == 0
    assert "/workspace/a.py" in result.output


def test_files_grep_lists_matches(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")
    monkeypatch.setattr(
        client_module.httpx,
        "request",
        lambda *a, **k: FakeResponse(
            200, json_data={"matches": [{"path": "/workspace/a.py", "line": 3, "text": "import os"}], "truncated": False}
        ),
    )

    result = runner.invoke(app, ["files", "grep", "import", "--session", "sess-1"])

    assert result.exit_code == 0
    assert "/workspace/a.py:3:import os" in result.output


# ── httpx error envelope translation ──────────────────────────────────────
def test_hosted_request_translates_control_plane_error_envelope(monkeypatch):
    def fake_request(method, url, **kwargs):
        return FakeResponse(404, json_data={"error": {"code": "not_found", "message": "Sandbox session not found"}})

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    with pytest.raises(CliError, match="Sandbox session not found"):
        client_module.hosted_request(_hosted_ctx(), "GET", "/v1/sandboxes/missing")


def test_local_request_translates_http_exception_detail(monkeypatch):
    def fake_request(method, url, **kwargs):
        return FakeResponse(404, json_data={"detail": "File not found: x.txt"})

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    ctx = Context(mode="local", sidecar_url="http://localhost:8080", sidecar_token="tok")
    with pytest.raises(CliError, match="File not found"):
        client_module.local_request(ctx, "POST", "/view", json={"path": "x.txt"})


# ── No pricing/plan language anywhere in --help output ────────────────────
COMMAND_HELP_PATHS = [
    [],
    ["up"],
    ["exec"],
    ["signup"],
    ["config"],
    ["config", "set-key"],
    ["config", "set-url"],
    ["config", "show"],
    ["session"],
    ["session", "create"],
    ["session", "ls"],
    ["session", "rm"],
    ["files"],
    ["files", "view"],
    ["files", "create"],
    ["files", "edit"],
    ["log"],
    ["watch"],
    ["secrets"],
    ["secrets", "create"],
    ["secrets", "ls"],
    ["secrets", "rm"],
    ["images"],
    ["images", "build"],
    ["images", "get"],
    ["images", "ls"],
    ["images", "rm"],
    ["volumes"],
    ["volumes", "create"],
    ["volumes", "get"],
    ["volumes", "ls"],
    ["volumes", "rm"],
    ["webhooks"],
    ["webhooks", "create"],
    ["webhooks", "ls"],
    ["webhooks", "rm"],
    ["webhooks", "deliveries"],
    ["mcp"],
    ["mcp", "init"],
]


@pytest.mark.parametrize("command_path", COMMAND_HELP_PATHS, ids=lambda p: " ".join(p) or "root")
def test_help_output_contains_no_pricing_language(command_path):
    result = runner.invoke(app, [*command_path, "--help"])

    assert result.exit_code == 0
    assert "$" not in result.output, f"'$' found in `boxkite {' '.join(command_path)} --help`"
    clean_output = _ANSI_ESCAPE_PATTERN.sub("", result.output)
    match = BANNED_WORD_PATTERN.search(clean_output)
    assert match is None, f"banned word {match.group(0)!r} found in `boxkite {' '.join(command_path)} --help`"


def test_no_banned_words_in_cli_source():
    """Static grep sweep over the CLI source itself (not just --help text),
    mirroring how control-plane/tests/test_usage_limits.py holds its own
    error strings to the same bar."""
    cli_dir = Path(__file__).resolve().parent.parent / "src" / "boxkite" / "cli"
    pattern = re.compile(r"\$|dollar|pricing|subscription|billing", re.IGNORECASE)
    offenders = []
    for py_file in cli_dir.glob("*.py"):
        text = py_file.read_text()
        for lineno, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                offenders.append(f"{py_file.name}:{lineno}: {line.strip()}")

    assert not offenders, "banned pricing language found:\n" + "\n".join(offenders)


# ── docker compose file discovery for `boxkite up` ───────────────────────
def test_up_fails_clearly_when_compose_file_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["up"])

    assert result.exit_code == 1
    assert "docker-compose.yml" in result.output


def test_up_finds_compose_file_via_explicit_flag(tmp_path):
    missing = tmp_path / "does-not-exist.yml"

    result = runner.invoke(app, ["up", "--compose-file", str(missing)])

    assert result.exit_code == 1
    assert "Compose file not found" in result.output


def test_httpx_available_for_sanity():
    # Guards against an environment where httpx isn't actually importable
    # even though it's declared as a dependency.
    assert httpx.__name__ == "httpx"
