"""Tests for the sidecar's `/exec` `secret_env` field (docs/SECRETS-DESIGN.md's
bash_tool addendum).

Covers:
- A resolved secret is merged into the exec'd process's environment (not
  the command string) -- verified by asserting the actual `env=` kwarg
  `exec_in_sandbox`'s subprocess call receives.
- A secret name not resolvable (not granted, or resolution failure) is
  silently omitted, not an error.
- The resolved value is scrubbed from stdout/stderr before the response is
  built, same as /http-request's `_scrub_secret_values`.
- No `secret_env` field at all behaves exactly as before (no `extra_env`
  passed to exec_in_sandbox).
"""

from __future__ import annotations

import main as sidecar_main
from fastapi.testclient import TestClient


def _client() -> TestClient:
    return TestClient(sidecar_main.app)


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: "the-real-secret"}


def test_secret_env_is_merged_into_the_exec_environment(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setitem(sidecar_main.current_session, "secret_names", ["claude-code-key"])
    sidecar_main._secret_value_cache.clear()

    async def _fake_get_secret_value(name):
        assert name == "claude-code-key"
        return "sk-ant-the-real-value"

    monkeypatch.setattr(sidecar_main, "_get_secret_value", _fake_get_secret_value)

    captured = {}

    async def _fake_exec_in_sandbox(command, timeout, extra_env=None):
        captured["extra_env"] = extra_env
        return (0, "ran fine", "")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox)

    client = _client()
    response = client.post(
        "/exec",
        json={
            "command": "claude -p hi",
            "timeout": 5,
            "secret_env": {"ANTHROPIC_API_KEY": "claude-code-key"},
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert captured["extra_env"] == {"ANTHROPIC_API_KEY": "sk-ant-the-real-value"}


def test_unresolvable_secret_name_is_silently_omitted(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    sidecar_main._secret_value_cache.clear()

    async def _fake_get_secret_value(name):
        return None  # not granted / resolution failed

    monkeypatch.setattr(sidecar_main, "_get_secret_value", _fake_get_secret_value)

    captured = {}

    async def _fake_exec_in_sandbox(command, timeout, extra_env=None):
        captured["extra_env"] = extra_env
        return (0, "ok", "")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox)

    client = _client()
    response = client.post(
        "/exec",
        json={"command": "echo hi", "timeout": 5, "secret_env": {"SOME_KEY": "not-granted"}},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert captured["extra_env"] is None


def test_validate_secret_env_var_name_rejects_denylisted_names():
    assert sidecar_main._validate_secret_env_var_name("ANTHROPIC_API_KEY") is True
    for dangerous in ("PATH", "LD_PRELOAD", "BASH_ENV", "PYTHONPATH", "path", "ld_preload"):
        assert sidecar_main._validate_secret_env_var_name(dangerous) is False


def test_validate_secret_env_var_name_rejects_malformed_identifiers():
    for malformed in ("", "FOO=BAR", "1STARTSWITHDIGIT", "HAS SPACE", "HAS-DASH", "HAS\nNEWLINE"):
        assert sidecar_main._validate_secret_env_var_name(malformed) is False


def test_secret_env_with_denylisted_key_is_silently_omitted(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setitem(sidecar_main.current_session, "secret_names", ["claude-code-key"])
    sidecar_main._secret_value_cache.clear()

    async def _fake_get_secret_value(name):
        # Should never be called -- the env var name is rejected before
        # any secret resolution is attempted.
        raise AssertionError("must not resolve a secret for a denylisted env var name")

    monkeypatch.setattr(sidecar_main, "_get_secret_value", _fake_get_secret_value)

    captured = {}

    async def _fake_exec_in_sandbox(command, timeout, extra_env=None):
        captured["extra_env"] = extra_env
        return (0, "ok", "")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox)

    client = _client()
    response = client.post(
        "/exec",
        json={"command": "echo hi", "timeout": 5, "secret_env": {"LD_PRELOAD": "claude-code-key"}},
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert captured["extra_env"] is None


def test_secret_env_mixed_valid_and_denylisted_keys_only_injects_valid_one(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setitem(sidecar_main.current_session, "secret_names", ["claude-code-key"])
    sidecar_main._secret_value_cache.clear()

    async def _fake_get_secret_value(name):
        assert name == "claude-code-key"
        return "sk-ant-the-real-value"

    monkeypatch.setattr(sidecar_main, "_get_secret_value", _fake_get_secret_value)

    captured = {}

    async def _fake_exec_in_sandbox(command, timeout, extra_env=None):
        captured["extra_env"] = extra_env
        return (0, "ok", "")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox)

    client = _client()
    response = client.post(
        "/exec",
        json={
            "command": "echo hi",
            "timeout": 5,
            "secret_env": {
                "ANTHROPIC_API_KEY": "claude-code-key",
                "PATH": "claude-code-key",
            },
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    assert captured["extra_env"] == {"ANTHROPIC_API_KEY": "sk-ant-the-real-value"}


def test_secret_value_is_scrubbed_from_exec_output(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")
    monkeypatch.setitem(sidecar_main.current_session, "secret_names", ["claude-code-key"])
    sidecar_main._secret_value_cache.clear()

    async def _fake_get_secret_value(name):
        return "sk-ant-the-real-value"

    monkeypatch.setattr(sidecar_main, "_get_secret_value", _fake_get_secret_value)

    async def _fake_exec_in_sandbox(command, timeout, extra_env=None):
        # Simulate a program that (accidentally or otherwise) echoes the
        # credential it was given back to stdout.
        return (0, f"using key sk-ant-the-real-value\n", "")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox)

    client = _client()
    response = client.post(
        "/exec",
        json={
            "command": "some-tool",
            "timeout": 5,
            "secret_env": {"ANTHROPIC_API_KEY": "claude-code-key"},
        },
        headers=_auth_headers(),
    )

    assert response.status_code == 200
    body = response.json()
    assert "sk-ant-the-real-value" not in body["stdout"]
    assert "[REDACTED_SECRET:claude-code-key]" in body["stdout"]


def test_no_secret_env_field_behaves_exactly_as_before(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", "the-real-secret")

    captured = {}

    async def _fake_exec_in_sandbox(command, timeout, extra_env=None):
        captured["extra_env"] = extra_env
        return (0, "hello", "")

    monkeypatch.setattr(sidecar_main, "exec_in_sandbox", _fake_exec_in_sandbox)

    client = _client()
    response = client.post(
        "/exec", json={"command": "echo hello", "timeout": 5}, headers=_auth_headers()
    )

    assert response.status_code == 200
    assert captured["extra_env"] is None
