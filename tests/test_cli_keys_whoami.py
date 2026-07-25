"""Tests for `boxkite keys ls/rm` and `boxkite whoami`. Same mocking
pattern as test_cli.py: httpx is monkeypatched, no real control-plane."""

from __future__ import annotations

from typer.testing import CliRunner

from boxkite.cli import app
from boxkite.cli import client as client_module
from boxkite.cli import cmd_keys
from boxkite.cli import config_store

runner = CliRunner()


class FakeResponse:
    def __init__(self, status_code: int, json_data=None, has_content: bool = True):
        self.status_code = status_code
        self._json = json_data
        self.content = b"x" if has_content else b""

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    config_dir = tmp_path / ".boxkite"
    monkeypatch.setattr(config_store, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(config_store, "LOCAL_ENV_FILE", config_dir / "local.env")
    yield


# ── boxkite keys ls/rm: JWT login flow, not the stored API key ──────────
def test_keys_ls_logs_in_and_lists_keys(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com")

    def fake_post(url, **kwargs):
        assert url == "https://cp.example.com/v1/auth/login"
        return FakeResponse(200, json_data={"access_token": "jwt-123", "expires_in": 1800})

    def fake_request(method, url, **kwargs):
        assert method == "GET"
        assert url == "https://cp.example.com/v1/api-keys"
        assert kwargs["headers"]["Authorization"] == "Bearer jwt-123"
        return FakeResponse(
            200,
            json_data=[
                {
                    "id": "key-1",
                    "name": "ci",
                    "prefix": "bxk_live_ab12",
                    "created_at": "2026-01-01T00:00:00Z",
                    "revoked_at": None,
                    "last_used_at": None,
                }
            ],
        )

    monkeypatch.setattr(cmd_keys.httpx, "post", fake_post)
    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(app, ["keys", "ls", "--email", "a@example.com", "--password", "secret123"])

    assert result.exit_code == 0
    assert "key-1" in result.output
    assert "bxk_live_ab12" in result.output


def test_keys_rm_logs_in_and_revokes(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com")

    def fake_post(url, **kwargs):
        return FakeResponse(200, json_data={"access_token": "jwt-123", "expires_in": 1800})

    def fake_request(method, url, **kwargs):
        assert method == "DELETE"
        assert url == "https://cp.example.com/v1/api-keys/key-1"
        return FakeResponse(204, has_content=False)

    monkeypatch.setattr(cmd_keys.httpx, "post", fake_post)
    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(
        app, ["keys", "rm", "key-1", "--email", "a@example.com", "--password", "secret123"]
    )

    assert result.exit_code == 0
    assert "key-1" in result.output


def test_keys_ls_requires_configured_url():
    result = runner.invoke(app, ["keys", "ls", "--email", "a@example.com", "--password", "x"])

    assert result.exit_code == 1
    assert "boxkite config set-url" in result.output


# ── boxkite whoami: uses the already-stored API key, no prompt ──────────
def test_whoami_shows_email_and_usage(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")

    def fake_request(method, url, **kwargs):
        if url.endswith("/v1/account"):
            return FakeResponse(
                200, json_data={"id": "acct-1", "email": "me@example.com", "created_at": "2026-01-01T00:00:00Z"}
            )
        if url.endswith("/v1/usage"):
            return FakeResponse(
                200,
                json_data={
                    "monthly_sandbox_hours_used": 1.5,
                    "monthly_sandbox_hours_limit": 20.0,
                    "concurrent_sandboxes": 1,
                    "concurrent_sandboxes_limit": 2,
                },
            )
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(app, ["whoami"])

    assert result.exit_code == 0
    assert "me@example.com" in result.output
    assert "1.5" in result.output
    assert "20.0" in result.output


def test_whoami_in_local_mode_explains_capability_gap():
    config_store.write_local_env(token="tok123", sidecar_url="http://localhost:8080")

    result = runner.invoke(app, ["whoami"])

    assert result.exit_code == 1
    assert "hosted" in result.output.lower()
