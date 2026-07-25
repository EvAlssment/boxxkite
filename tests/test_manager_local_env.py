"""SandboxManager compose-mode fallback: auto-load the sidecar token + URL from
~/.boxkite/local.env (written by `boxkite up`) so callers don't have to export
them by hand. Explicit env vars still take precedence.
"""

from __future__ import annotations

import boxkite.local_env as local_env
from boxkite import SandboxManager


def _write_local_env(tmp_path, token="filetoken", url="http://localhost:9999"):
    f = tmp_path / "local.env"
    f.write_text(f"SIDECAR_AUTH_TOKEN={token}\nSIDECAR_URL={url}\n")
    return f


def test_compose_mode_loads_token_and_url_from_local_env(tmp_path, monkeypatch):
    monkeypatch.setattr(local_env, "LOCAL_ENV_FILE", _write_local_env(tmp_path))
    monkeypatch.setenv("RUNTIME_MODE", "compose")
    monkeypatch.delenv("SIDECAR_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("SIDECAR_URL", raising=False)

    manager = SandboxManager()

    assert manager._compose_auth_token == "filetoken"
    assert manager._compose_url == "http://localhost:9999"


def test_explicit_env_vars_win_over_local_env(tmp_path, monkeypatch):
    monkeypatch.setattr(local_env, "LOCAL_ENV_FILE", _write_local_env(tmp_path))
    monkeypatch.setenv("RUNTIME_MODE", "compose")
    monkeypatch.setenv("SIDECAR_AUTH_TOKEN", "envtoken")
    monkeypatch.setenv("SIDECAR_URL", "http://localhost:8080")

    manager = SandboxManager()

    assert manager._compose_auth_token == "envtoken"
    assert manager._compose_url == "http://localhost:8080"
