"""Tests for `boxkite log`/`boxkite watch` — hosted-only audit-log history
and live-watch commands. Same mocking pattern as test_cli.py: httpx is
monkeypatched, no real control-plane."""

from __future__ import annotations

import pytest
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


class FakeStreamResponse:
    """Stands in for the `with httpx.stream(...) as resp:` context manager
    `hosted_stream_events` uses -- a plain FakeResponse can't be used as a
    context manager, so this is its own small fixture."""

    def __init__(self, status_code: int, lines: list[str], json_data=None):
        self.status_code = status_code
        self._lines = lines
        self._json = json_data

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def iter_lines(self):
        yield from self._lines

    def read(self) -> None:
        pass

    def json(self):
        if self._json is None:
            raise ValueError("no json body")
        return self._json


ENTRY = {
    "id": "log-1",
    "session_id": "sess-1",
    "source": "agent",
    "operation": "exec",
    "detail": {"command": "echo hi"},
    "exit_code": 0,
    "output_truncated": None,
    "started_at": "2026-01-01T00:00:00Z",
    "duration_ms": 12,
}


@pytest.fixture(autouse=True)
def _isolated_config(tmp_path, monkeypatch):
    config_dir = tmp_path / ".boxkite"
    monkeypatch.setattr(config_store, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(config_store, "CONFIG_FILE", config_dir / "config.toml")
    monkeypatch.setattr(config_store, "LOCAL_ENV_FILE", config_dir / "local.env")
    yield


# ── boxkite log ────────────────────────────────────────────────────────
def test_log_hosted_auto_selects_single_session_and_prints_entries(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")

    def fake_request(method, url, **kwargs):
        if url.endswith("/v1/sandboxes") and method == "GET":
            return FakeResponse(200, json_data=[{"id": "sess-1", "status": "active"}])
        if url.endswith("/v1/sandboxes/sess-1/log"):
            assert kwargs["params"] == {"limit": 50, "offset": 0}
            return FakeResponse(200, json_data={"entries": [ENTRY], "limit": 50, "offset": 0, "total": 1})
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(app, ["log"])

    assert result.exit_code == 0
    assert "echo hi" in result.output
    assert "exit=0" in result.output
    assert "(1 of 1 total, offset=0)" in result.output


def test_log_passes_limit_and_offset(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")

    def fake_request(method, url, **kwargs):
        if url.endswith("/v1/sandboxes"):
            return FakeResponse(200, json_data=[{"id": "sess-1", "status": "active"}])
        if url.endswith("/v1/sandboxes/sess-1/log"):
            assert kwargs["params"] == {"limit": 10, "offset": 5}
            return FakeResponse(200, json_data={"entries": [], "limit": 10, "offset": 5, "total": 0})
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(app, ["log", "--limit", "10", "--offset", "5"])

    assert result.exit_code == 0
    assert "No log entries." in result.output


def test_log_respects_explicit_session_over_auto_select(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")

    def fake_request(method, url, **kwargs):
        if url.endswith("/v1/sandboxes"):
            raise AssertionError("should not list sessions when --session is given")
        if url.endswith("/v1/sandboxes/explicit-id/log"):
            return FakeResponse(200, json_data={"entries": [ENTRY], "limit": 50, "offset": 0, "total": 1})
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(client_module.httpx, "request", fake_request)

    result = runner.invoke(app, ["log", "--session", "explicit-id"])

    assert result.exit_code == 0
    assert "echo hi" in result.output


def test_log_in_local_mode_explains_capability_gap():
    config_store.write_local_env(token="tok123", sidecar_url="http://localhost:8080")

    result = runner.invoke(app, ["log"])

    assert result.exit_code == 1
    assert "hosted control-plane" in result.output


# ── boxkite watch ──────────────────────────────────────────────────────
def test_watch_hosted_prints_each_streamed_entry(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")

    def fake_request(method, url, **kwargs):
        if url.endswith("/v1/sandboxes"):
            return FakeResponse(200, json_data=[{"id": "sess-1", "status": "active"}])
        raise AssertionError(f"unexpected request: {method} {url}")

    sse_lines = [
        "id: log-1",
        '{"id": "log-1", "session_id": "sess-1", "source": "agent", "operation": "exec", '
        '"detail": {"command": "echo hi"}, "exit_code": 0, "output_truncated": null, '
        '"started_at": "2026-01-01T00:00:00Z", "duration_ms": 12}',
    ]
    # The parser only reads lines that start with "data:" -- rebuild the raw
    # SSE frame that shape (matches control-plane's `_log_entry_sse_event`).
    sse_lines[1] = f"data: {sse_lines[1]}"
    sse_lines.append("")

    def fake_stream(method, url, **kwargs):
        assert url == "https://cp.example.com/v1/sandboxes/sess-1/watch"
        assert kwargs["headers"]["Authorization"] == "Bearer bxk_live_x"
        assert kwargs["timeout"] is None
        return FakeStreamResponse(200, sse_lines)

    monkeypatch.setattr(client_module.httpx, "request", fake_request)
    monkeypatch.setattr(client_module.httpx, "stream", fake_stream)

    result = runner.invoke(app, ["watch"])

    assert result.exit_code == 0
    assert "echo hi" in result.output
    assert "exit=0" in result.output


def test_watch_respects_explicit_session_over_auto_select(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")

    def fake_request(method, url, **kwargs):
        raise AssertionError("should not list sessions when --session is given")

    def fake_stream(method, url, **kwargs):
        assert url == "https://cp.example.com/v1/sandboxes/explicit-id/watch"
        return FakeStreamResponse(200, [])

    monkeypatch.setattr(client_module.httpx, "request", fake_request)
    monkeypatch.setattr(client_module.httpx, "stream", fake_stream)

    result = runner.invoke(app, ["watch", "--session", "explicit-id"])

    assert result.exit_code == 0


def test_watch_translates_error_response(monkeypatch):
    config_store.write_hosted_config(base_url="https://cp.example.com", api_key="bxk_live_x")

    def fake_request(method, url, **kwargs):
        return FakeResponse(200, json_data=[{"id": "sess-1", "status": "active"}])

    def fake_stream(method, url, **kwargs):
        return FakeStreamResponse(404, [], json_data={"error": {"code": "not_found", "message": "Sandbox session not found"}})

    monkeypatch.setattr(client_module.httpx, "request", fake_request)
    monkeypatch.setattr(client_module.httpx, "stream", fake_stream)

    result = runner.invoke(app, ["watch"])

    assert result.exit_code == 1
    assert "Sandbox session not found" in result.output


def test_watch_in_local_mode_explains_capability_gap():
    config_store.write_local_env(token="tok123", sidecar_url="http://localhost:8080")

    result = runner.invoke(app, ["watch"])

    assert result.exit_code == 1
    assert "hosted control-plane" in result.output
