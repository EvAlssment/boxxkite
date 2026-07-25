"""Tests for `boxkite secrets/images/volumes/webhooks` — hosted-mode CRUD
CLI commands backed by the account API key (same auth as `session`/`exec`).
Same mocking pattern as test_cli.py: httpx is monkeypatched, no real
control-plane."""

from __future__ import annotations

from typer.testing import CliRunner

from boxkite.cli import app
from boxkite.cli import client as client_module
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


def _hosted():
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")


# ── secrets ──────────────────────────────────────────────────────────────
def test_secrets_create_never_echoes_value(monkeypatch):
    _hosted()
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return FakeResponse(
            201,
            json_data={
                "id": "sec-1",
                "name": "openai-key",
                "allowed_hosts": ["api.openai.com"],
                "created_at": "2026-01-01T00:00:00Z",
                "last_used_at": None,
            },
        )

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(
        app,
        [
            "secrets",
            "create",
            "openai-key",
            "--value",
            "sk-super-secret-value",
            "--allowed-hosts",
            "api.openai.com",
        ],
    )

    assert result.exit_code == 0
    assert "sec-1" in result.output
    assert "sk-super-secret-value" not in result.output
    assert captured["method"] == "POST"
    assert captured["url"] == "https://cp.example.com/v1/secrets"
    assert captured["json"]["value"] == "sk-super-secret-value"
    assert captured["json"]["allowed_hosts"] == ["api.openai.com"]


def test_secrets_create_passes_trust_tier(monkeypatch):
    _hosted()
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["json"] = kwargs.get("json")
        return FakeResponse(
            201,
            json_data={
                "id": "sec-1",
                "name": "audit-agent-testnet",
                "allowed_hosts": ["sepolia.infura.io"],
                "trust_tier": "testnet",
                "created_at": "2026-01-01T00:00:00Z",
                "last_used_at": None,
            },
        )

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(
        app,
        [
            "secrets",
            "create",
            "audit-agent-testnet",
            "--value",
            "0xdeadbeef",
            "--allowed-hosts",
            "sepolia.infura.io",
            "--trust-tier",
            "testnet",
        ],
    )

    assert result.exit_code == 0
    assert "trust_tier='testnet'" in result.output
    assert captured["json"]["trust_tier"] == "testnet"


def test_secrets_create_omits_trust_tier_when_not_passed(monkeypatch):
    _hosted()
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["json"] = kwargs.get("json")
        return FakeResponse(
            201,
            json_data={
                "id": "sec-1",
                "name": "openai-key",
                "allowed_hosts": ["api.openai.com"],
                "trust_tier": None,
                "created_at": "2026-01-01T00:00:00Z",
                "last_used_at": None,
            },
        )

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(
        app,
        ["secrets", "create", "openai-key", "--value", "sk-x", "--allowed-hosts", "api.openai.com"],
    )

    assert result.exit_code == 0
    assert "trust_tier" not in captured["json"]


def test_secrets_ls_lists_without_value(monkeypatch):
    _hosted()
    monkeypatch.setattr(
        client_module.httpx,
        "request",
        lambda *a, **k: FakeResponse(
            200,
            json_data=[
                {
                    "id": "sec-1",
                    "name": "openai-key",
                    "allowed_hosts": ["api.openai.com"],
                    "created_at": "2026-01-01T00:00:00Z",
                    "last_used_at": None,
                }
            ],
        ),
    )

    result = runner.invoke(app, ["secrets", "ls"])

    assert result.exit_code == 0
    assert "sec-1" in result.output
    assert "openai-key" in result.output


def test_secrets_rm_deletes(monkeypatch):
    _hosted()
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        return FakeResponse(204, has_content=False)

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(app, ["secrets", "rm", "sec-1"])

    assert result.exit_code == 0
    assert captured["method"] == "DELETE"
    assert captured["url"] == "https://cp.example.com/v1/secrets/sec-1"


def test_secrets_requires_hosted_mode():
    config_store.write_local_env(token="tok123", sidecar_url="http://localhost:8080")

    result = runner.invoke(app, ["secrets", "ls"])

    assert result.exit_code == 1
    assert "hosted control-plane" in result.output


# ── images ───────────────────────────────────────────────────────────────
def test_images_build_sends_pinned_packages(monkeypatch):
    _hosted()
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return FakeResponse(
            202, json_data={"id": "img-1", "label": None, "status": "queued", "created_at": "2026-01-01T00:00:00Z"}
        )

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(
        app,
        [
            "images",
            "build",
            "--base",
            "boxkite-minimal",
            "--python-package",
            "polars==1.9.0",
        ],
    )

    assert result.exit_code == 0
    assert "img-1" in result.output
    assert captured["method"] == "POST"
    assert captured["url"] == "https://cp.example.com/v1/images"
    assert captured["json"]["base"] == "boxkite-minimal"
    assert captured["json"]["python_packages"] == ["polars==1.9.0"]


def test_images_ls_lists_images(monkeypatch):
    _hosted()
    monkeypatch.setattr(
        client_module.httpx,
        "request",
        lambda *a, **k: FakeResponse(
            200,
            json_data=[
                {
                    "id": "img-1",
                    "label": None,
                    "base": "boxkite-default",
                    "python_packages": [],
                    "apt_packages": [],
                    "npm_packages": [],
                    "status": "completed",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
        ),
    )

    result = runner.invoke(app, ["images", "ls"])

    assert result.exit_code == 0
    assert "img-1" in result.output
    assert "completed" in result.output


def test_images_disabled_deployment_surfaces_server_404(monkeypatch):
    _hosted()
    monkeypatch.setattr(
        client_module.httpx,
        "request",
        lambda *a, **k: FakeResponse(
            404,
            json_data={
                "error": {
                    "code": "not_found",
                    "message": "The declarative image builder is not enabled on this deployment.",
                }
            },
        ),
    )

    result = runner.invoke(app, ["images", "ls"])

    assert result.exit_code == 1
    assert "not enabled on this deployment" in result.output


# ── volumes ──────────────────────────────────────────────────────────────
def test_volumes_create_sends_size_gb(monkeypatch):
    _hosted()
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return FakeResponse(
            202, json_data={"id": "vol-1", "label": None, "status": "queued", "created_at": "2026-01-01T00:00:00Z"}
        )

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(app, ["volumes", "create", "--size-gb", "10"])

    assert result.exit_code == 0
    assert "vol-1" in result.output
    assert captured["json"]["size_gb"] == 10.0


def test_volumes_ls_lists_volumes(monkeypatch):
    _hosted()
    monkeypatch.setattr(
        client_module.httpx,
        "request",
        lambda *a, **k: FakeResponse(
            200,
            json_data=[
                {
                    "id": "vol-1",
                    "label": "data",
                    "size_gb": 10.0,
                    "status": "ready",
                    "pvc_name": "pvc-1",
                    "created_at": "2026-01-01T00:00:00Z",
                }
            ],
        ),
    )

    result = runner.invoke(app, ["volumes", "ls"])

    assert result.exit_code == 0
    assert "vol-1" in result.output
    assert "ready" in result.output


# ── webhooks ─────────────────────────────────────────────────────────────
def test_webhooks_create_prints_secret_once(monkeypatch):
    _hosted()
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        captured["json"] = kwargs.get("json")
        return FakeResponse(
            201,
            json_data={
                "id": "wh-1",
                "url": "https://example.com/hook",
                "event_types": ["sandbox.created"],
                "description": None,
                "is_active": True,
                "created_at": "2026-01-01T00:00:00Z",
                "last_triggered_at": None,
                "secret": "whsec_abc123",
            },
        )

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(
        app,
        ["webhooks", "create", "https://example.com/hook", "--event-type", "sandbox.created"],
    )

    assert result.exit_code == 0
    assert "wh-1" in result.output
    assert "whsec_abc123" in result.output
    assert captured["json"]["url"] == "https://example.com/hook"
    assert captured["json"]["event_types"] == ["sandbox.created"]


def test_webhooks_ls_never_shows_secret(monkeypatch):
    _hosted()
    monkeypatch.setattr(
        client_module.httpx,
        "request",
        lambda *a, **k: FakeResponse(
            200,
            json_data=[
                {
                    "id": "wh-1",
                    "url": "https://example.com/hook",
                    "event_types": ["sandbox.created"],
                    "description": None,
                    "is_active": True,
                    "created_at": "2026-01-01T00:00:00Z",
                    "last_triggered_at": None,
                }
            ],
        ),
    )

    result = runner.invoke(app, ["webhooks", "ls"])

    assert result.exit_code == 0
    assert "wh-1" in result.output
    assert "whsec" not in result.output


def test_webhooks_rm_deletes(monkeypatch):
    _hosted()
    captured = {}

    def fake_request(method, url, **kwargs):
        captured["method"] = method
        captured["url"] = url
        return FakeResponse(204, has_content=False)

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(app, ["webhooks", "rm", "wh-1"])

    assert result.exit_code == 0
    assert captured["method"] == "DELETE"
    assert captured["url"] == "https://cp.example.com/v1/webhooks/wh-1"


def test_webhooks_deliveries_lists_attempts(monkeypatch):
    _hosted()
    monkeypatch.setattr(
        client_module.httpx,
        "request",
        lambda *a, **k: FakeResponse(
            200,
            json_data=[
                {
                    "id": "del-1",
                    "event_type": "sandbox.created",
                    "status": "delivered",
                    "attempt_count": 1,
                    "next_attempt_at": "2026-01-01T00:00:00Z",
                    "last_attempt_at": "2026-01-01T00:00:00Z",
                    "response_status_code": 200,
                    "failure_reason": None,
                    "created_at": "2026-01-01T00:00:00Z",
                    "delivered_at": "2026-01-01T00:00:00Z",
                }
            ],
        ),
    )

    result = runner.invoke(app, ["webhooks", "deliveries", "wh-1"])

    assert result.exit_code == 0
    assert "del-1" in result.output
    assert "delivered" in result.output
