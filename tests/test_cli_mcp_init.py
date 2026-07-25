"""Tests for `boxkite mcp init <target>`. No real MCP client or control-plane
involved -- only the JSON/TOML config-file merge-write logic and the
hosted-config precondition are exercised."""

from __future__ import annotations

import json
import tomllib

import pytest
import tomli_w
from typer.testing import CliRunner

from boxkite.cli import app, cmd_mcp, config_store

runner = CliRunner()


def _load_config(path):
    """Read a target's config file with whichever format it actually uses."""
    text = path.read_text()
    return tomllib.loads(text) if path.suffix == ".toml" else json.loads(text)


def _dump_config(path, data):
    """Write a target's config file with whichever format it actually uses."""
    path.write_text(tomli_w.dumps(data) if path.suffix == ".toml" else json.dumps(data))


def _servers_key(path) -> str:
    return "mcp_servers" if path.suffix == ".toml" else "mcpServers"


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    config_dir = tmp_path / ".boxkite"
    monkeypatch.setattr(config_store, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(config_store, "LOCAL_ENV_FILE", config_dir / "local.env")
    # Both Path.cwd() (claude-code) and Path.home() (cursor/windsurf/claude-desktop)
    # need to resolve inside tmp_path so nothing touches the real machine's
    # actual MCP client config files.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HOME", str(tmp_path))
    yield


UNRELATED_ENTRY = {"other-server": {"command": "some-other-mcp-server"}}


@pytest.mark.parametrize("target", list(cmd_mcp.TARGETS))
def test_mcp_init_merges_boxkite_entry_without_clobbering_others(target):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_abc")

    path = cmd_mcp._config_path_for_target(target)
    path.parent.mkdir(parents=True, exist_ok=True)
    servers_key = _servers_key(path)
    _dump_config(path, {servers_key: dict(UNRELATED_ENTRY), "someUnrelatedTopLevelKey": True})

    result = runner.invoke(app, ["mcp", "init", target])

    assert result.exit_code == 0, result.output
    assert "Added" in result.output

    data = _load_config(path)
    assert data["someUnrelatedTopLevelKey"] is True
    assert data[servers_key]["other-server"] == UNRELATED_ENTRY["other-server"]
    assert data[servers_key]["boxkite"] == {
        "command": "boxkite-mcp",
        "env": {
            "BOXKITE_BASE_URL": "https://cp.example.com",
            "BOXKITE_API_KEY": "bxk_live_abc",
        },
    }


def test_mcp_init_requires_hosted_config():
    result = runner.invoke(app, ["mcp", "init", "cursor"])

    assert result.exit_code == 1
    assert "boxkite signup" in result.output


def test_mcp_init_is_idempotent_on_already_configured_file():
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_abc")

    path = cmd_mcp._config_path_for_target("windsurf")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "boxkite": {
                        "command": "boxkite-mcp",
                        "env": {
                            "BOXKITE_BASE_URL": "https://cp.example.com",
                            "BOXKITE_API_KEY": "bxk_live_abc",
                        },
                    },
                    **UNRELATED_ENTRY,
                }
            }
        )
    )

    result = runner.invoke(app, ["mcp", "init", "windsurf"])

    assert result.exit_code == 0, result.output
    assert "Updated" in result.output

    data = json.loads(path.read_text())
    assert data["mcpServers"]["other-server"] == UNRELATED_ENTRY["other-server"]
    assert data["mcpServers"]["boxkite"]["env"]["BOXKITE_API_KEY"] == "bxk_live_abc"


def test_mcp_init_is_idempotent_on_already_configured_codex_toml_file():
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_abc")

    path = cmd_mcp._config_path_for_target("codex")
    path.parent.mkdir(parents=True, exist_ok=True)
    _dump_config(
        path,
        {
            "mcp_servers": {
                "boxkite": {
                    "command": "boxkite-mcp",
                    "env": {
                        "BOXKITE_BASE_URL": "https://cp.example.com",
                        "BOXKITE_API_KEY": "bxk_live_abc",
                    },
                },
                **UNRELATED_ENTRY,
            }
        },
    )

    result = runner.invoke(app, ["mcp", "init", "codex"])

    assert result.exit_code == 0, result.output
    assert "Updated" in result.output

    data = tomllib.loads(path.read_text())
    assert data["mcp_servers"]["other-server"] == UNRELATED_ENTRY["other-server"]
    assert data["mcp_servers"]["boxkite"]["env"]["BOXKITE_API_KEY"] == "bxk_live_abc"


def test_mcp_init_refuses_to_overwrite_invalid_toml_file():
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_abc")

    path = cmd_mcp._config_path_for_target("codex")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid toml [[[")

    result = runner.invoke(app, ["mcp", "init", "codex"])

    assert result.exit_code == 1
    assert "not valid TOML" in result.output


def test_mcp_init_rejects_unknown_target():
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_abc")

    result = runner.invoke(app, ["mcp", "init", "vscode"])

    assert result.exit_code == 1
    assert "Unknown target" in result.output


def test_mcp_init_refuses_to_overwrite_non_json_object_file():
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_abc")

    path = cmd_mcp._config_path_for_target("cursor")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not valid json")

    result = runner.invoke(app, ["mcp", "init", "cursor"])

    assert result.exit_code == 1
    assert "not valid JSON" in result.output
