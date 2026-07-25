from __future__ import annotations

import pytest

from boxkite_bastion.config import BastionConfigError, BastionSettings, _normalize_to_ws_origin


def test_normalize_https_to_wss():
    assert _normalize_to_ws_origin("https://api.example.com") == "wss://api.example.com"


def test_normalize_http_to_ws():
    assert _normalize_to_ws_origin("http://localhost:8000") == "ws://localhost:8000"


def test_normalize_leaves_ws_scheme_untouched():
    assert _normalize_to_ws_origin("ws://localhost:8000") == "ws://localhost:8000"


def test_normalize_leaves_wss_scheme_untouched():
    assert _normalize_to_ws_origin("wss://api.example.com") == "wss://api.example.com"


def test_normalize_strips_trailing_slash():
    assert _normalize_to_ws_origin("https://api.example.com/") == "wss://api.example.com"


def test_normalize_rejects_unrecognized_scheme():
    with pytest.raises(BastionConfigError):
        _normalize_to_ws_origin("ftp://api.example.com")


def test_from_env_requires_control_plane_url(monkeypatch):
    monkeypatch.delenv("BOXKITE_BASTION_CONTROL_PLANE_URL", raising=False)
    with pytest.raises(BastionConfigError):
        BastionSettings.from_env()


def test_from_env_reads_and_normalizes_control_plane_url(monkeypatch):
    monkeypatch.setenv("BOXKITE_BASTION_CONTROL_PLANE_URL", "https://api.example.com")
    settings = BastionSettings.from_env()
    assert settings.control_plane_ws_base_url == "wss://api.example.com"
    assert settings.listen_port == 2222
    assert settings.host_key_path is None
    assert settings.max_connections_per_host == 10
    assert settings.login_timeout_seconds == 30.0


def test_from_env_reads_optional_overrides(monkeypatch):
    monkeypatch.setenv("BOXKITE_BASTION_CONTROL_PLANE_URL", "https://api.example.com")
    monkeypatch.setenv("BOXKITE_BASTION_LISTEN_HOST", "127.0.0.1")
    monkeypatch.setenv("BOXKITE_BASTION_LISTEN_PORT", "2200")
    monkeypatch.setenv("BOXKITE_BASTION_HOST_KEY_PATH", "/etc/bastion/host_key")
    monkeypatch.setenv("BOXKITE_BASTION_MAX_CONNECTIONS_PER_HOST", "3")
    monkeypatch.setenv("BOXKITE_BASTION_LOGIN_TIMEOUT_SECONDS", "5")
    settings = BastionSettings.from_env()
    assert settings.listen_host == "127.0.0.1"
    assert settings.listen_port == 2200
    assert settings.host_key_path == "/etc/bastion/host_key"
    assert settings.max_connections_per_host == 3
    assert settings.login_timeout_seconds == 5.0
