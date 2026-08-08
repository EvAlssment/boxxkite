"""Tests for `boxxkite handoff <tool>`. No real sandbox or control-plane
involved -- a fake adapter and a monkeypatched create_handoff_sandbox
exercise the command's own argument/context/error-handling logic only."""

from __future__ import annotations

import pytest
from boxxkite_client.exceptions import BoxxkiteConnectionError
from typer.testing import CliRunner

from boxxkite.cli import app, cmd_handoff, config_store
from boxxkite.handoff.adapters import ADAPTERS
from boxxkite.handoff.core import Credential, HandoffError, LocatedSession

runner = CliRunner()


class FakeAdapter:
    name = "fake-tool"

    def __init__(self, *, raise_error: bool = False) -> None:
        self.raise_error = raise_error

    def locate_session(self, *, session_ref=None):
        if self.raise_error:
            raise HandoffError("no local session found")
        return LocatedSession(
            tool=self.name,
            session_id="s1",
            files=(),
            credential=Credential(env_var="TOOL_TOKEN", value="tok"),
            resume_command="fake-tool --resume s1",
            workdir="/workspace",
        )


@pytest.fixture(autouse=True)
def registered_fake_adapter():
    ADAPTERS["fake-tool"] = FakeAdapter
    yield
    ADAPTERS.pop("fake-tool", None)


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    """No hosted config on disk unless a test writes one -- matches
    test_cli_mcp_init.py's isolation pattern so this suite can't pick up
    the real developer machine's ~/.boxxkite/config.toml."""
    config_dir = tmp_path / ".boxxkite"
    monkeypatch.setattr(config_store, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(config_store, "LOCAL_ENV_FILE", config_dir / "local.env")


def test_handoff_requires_some_target_without_flags() -> None:
    """Nothing configured at all -- resolve_context()'s own generic error
    fires before cmd_handoff ever gets a mode to inspect."""
    result = runner.invoke(app, ["handoff", "fake-tool"])

    assert result.exit_code == 1
    assert "No boxxkite target configured" in result.output


def test_handoff_rejects_local_mode(monkeypatch) -> None:
    """Local docker-compose mode (`boxxkite up`) is configured, but handoff
    specifically needs a hosted target for its takeover WebSocket."""
    config_store.LOCAL_ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    config_store.LOCAL_ENV_FILE.write_text("SIDECAR_URL=http://localhost:8080\nSIDECAR_AUTH_TOKEN=tok\n")

    result = runner.invoke(app, ["handoff", "fake-tool"])

    assert result.exit_code == 1
    assert "hosted control-plane target" in result.output


def test_handoff_accepts_explicit_api_key_and_base_url_without_any_config(monkeypatch) -> None:
    captured = {}

    def _fake_create(client, session, **kwargs):
        captured["session"] = session
        raise BoxxkiteConnectionError("could not connect")  # short-circuit before a real WS/PTY loop

    monkeypatch.setattr(cmd_handoff, "create_handoff_sandbox", _fake_create)

    result = runner.invoke(
        app, ["handoff", "fake-tool", "--api-key", "key123", "--base-url", "https://example.test"]
    )

    assert result.exit_code == 1
    assert captured["session"].session_id == "s1"


def test_handoff_reports_adapter_handoff_error_without_a_raw_traceback() -> None:
    ADAPTERS["fake-tool"] = lambda: FakeAdapter(raise_error=True)

    result = runner.invoke(
        app, ["handoff", "fake-tool", "--api-key", "key123", "--base-url", "https://example.test"]
    )

    assert result.exit_code == 1
    assert "no local session found" in result.output


def test_handoff_reports_boxxkite_client_errors_without_a_raw_traceback(monkeypatch) -> None:
    """create_handoff_sandbox talks to a real BoxxkiteClient, which can raise
    its own exception hierarchy (bad api key, unreachable base_url, a 4xx/5xx
    from the control plane) -- these must be caught and reported the same
    clean way as an adapter's own HandoffError, not left to propagate as an
    unhandled traceback."""

    def _boom(*_args, **_kwargs):
        raise BoxxkiteConnectionError("could not connect")

    monkeypatch.setattr(cmd_handoff, "create_handoff_sandbox", _boom)

    result = runner.invoke(
        app, ["handoff", "fake-tool", "--api-key", "key123", "--base-url", "https://example.test"]
    )

    assert result.exit_code == 1
    assert "could not connect" in result.output


def test_handoff_rejects_unknown_tool() -> None:
    result = runner.invoke(
        app, ["handoff", "not-a-real-tool", "--api-key", "key123", "--base-url", "https://example.test"]
    )

    assert result.exit_code == 1
    assert "Unknown tool" in result.output
