"""Tests for k8s_auth.py's RotatingServiceAccountToken (part of #9's fix:
the control-plane's external, non-in-cluster credential used to be a
static, never-expiring token with no rotation cadence -- this self-mints a
short-lived replacement via the Kubernetes TokenRequest API and keeps it
fresh, using the API's own async refresh_api_key_hook mechanism rather than
a separately-managed background task.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from kubernetes_asyncio import client

from boxkite.k8s_auth import (
    RotatingServiceAccountToken,
    _enable_external_token_rotation,
)


def _make_configuration(initial_token: str = "bootstrap-token") -> client.Configuration:
    configuration = client.Configuration()
    configuration.host = "https://example-cluster.invalid"
    configuration.api_key["BearerToken"] = initial_token
    configuration.api_key_prefix["BearerToken"] = "Bearer"
    return configuration


def _fake_token_request_result(token: str, expires_in: timedelta) -> object:
    from types import SimpleNamespace

    return SimpleNamespace(
        status=SimpleNamespace(
            token=token,
            expiration_timestamp=datetime.now(timezone.utc) + expires_in,
        )
    )


async def test_first_call_mints_a_token_and_applies_it(monkeypatch):
    configuration = _make_configuration()
    hook = RotatingServiceAccountToken(
        service_account_name="boxkite-control-plane", namespace="sandbox", expiration_seconds=3600
    )

    call_count = 0

    async def fake_create_token(self, **kwargs):
        nonlocal call_count
        call_count += 1
        assert kwargs["name"] == "boxkite-control-plane"
        assert kwargs["namespace"] == "sandbox"
        return _fake_token_request_result("minted-token-1", timedelta(hours=1))

    monkeypatch.setattr(
        client.CoreV1Api, "create_namespaced_service_account_token", fake_create_token
    )

    await hook(configuration)

    assert call_count == 1
    assert configuration.api_key["BearerToken"] == "minted-token-1"
    assert hook.expires_at is not None


async def test_second_call_within_margin_does_not_re_mint(monkeypatch):
    configuration = _make_configuration()
    hook = RotatingServiceAccountToken(
        service_account_name="boxkite-control-plane", namespace="sandbox", expiration_seconds=3600
    )

    call_count = 0

    async def fake_create_token(self, **kwargs):
        nonlocal call_count
        call_count += 1
        return _fake_token_request_result(f"minted-token-{call_count}", timedelta(hours=1))

    monkeypatch.setattr(
        client.CoreV1Api, "create_namespaced_service_account_token", fake_create_token
    )

    await hook(configuration)
    await hook(configuration)
    await hook(configuration)

    assert call_count == 1
    assert configuration.api_key["BearerToken"] == "minted-token-1"


async def test_re_mints_once_expiry_margin_is_reached(monkeypatch):
    configuration = _make_configuration()
    hook = RotatingServiceAccountToken(
        service_account_name="boxkite-control-plane", namespace="sandbox", expiration_seconds=3600
    )

    call_count = 0

    async def fake_create_token(self, **kwargs):
        nonlocal call_count
        call_count += 1
        return _fake_token_request_result(f"minted-token-{call_count}", timedelta(hours=1))

    monkeypatch.setattr(
        client.CoreV1Api, "create_namespaced_service_account_token", fake_create_token
    )

    await hook(configuration)
    assert call_count == 1

    # Simulate time passing: the previously-minted token is now within the
    # refresh margin of its own expiry.
    hook._expires_at = datetime.now(timezone.utc) + timedelta(minutes=1)

    await hook(configuration)
    assert call_count == 2
    assert configuration.api_key["BearerToken"] == "minted-token-2"


async def test_mint_failure_is_swallowed_and_old_token_kept(monkeypatch, caplog):
    configuration = _make_configuration(initial_token="still-the-old-token")
    hook = RotatingServiceAccountToken(
        service_account_name="boxkite-control-plane", namespace="sandbox", expiration_seconds=3600
    )

    async def failing_create_token(self, **kwargs):
        raise RuntimeError("simulated API server failure")

    monkeypatch.setattr(
        client.CoreV1Api, "create_namespaced_service_account_token", failing_create_token
    )

    await hook(configuration)  # must not raise

    assert configuration.api_key["BearerToken"] == "still-the-old-token"
    assert hook.expires_at is None  # never successfully minted, so still "needs refresh"


async def test_does_not_deadlock_when_the_mint_call_itself_triggers_the_hook(monkeypatch):
    """Regression test: kubernetes_asyncio calls refresh_api_key_hook on
    EVERY outgoing request, including the TokenRequest call the hook itself
    makes. If that inner call reused the same hook-carrying Configuration,
    it would re-enter __call__ and try to re-acquire the same (non-
    reentrant) asyncio.Lock __call__ is already holding -- deadlocking
    forever. _mint_and_apply must authenticate its own TokenRequest call
    through a hook-less copy instead. Bounded with wait_for so a real
    deadlock fails this test instead of hanging the suite."""
    configuration = _make_configuration()
    hook = RotatingServiceAccountToken(
        service_account_name="boxkite-control-plane", namespace="sandbox", expiration_seconds=3600
    )
    configuration.refresh_api_key_hook = hook

    async def fake_create_token(self, **kwargs):
        # Mirror what kubernetes_asyncio's REST layer actually does before
        # issuing a request: ask the configuration for its (possibly
        # hook-refreshed) bearer token.
        await self.api_client.configuration.get_api_key_with_prefix("BearerToken")
        return _fake_token_request_result("minted-token", timedelta(hours=1))

    monkeypatch.setattr(
        client.CoreV1Api, "create_namespaced_service_account_token", fake_create_token
    )

    await asyncio.wait_for(hook(configuration), timeout=2.0)

    assert configuration.api_key["BearerToken"] == "minted-token"


async def test_enable_external_token_rotation_sets_hook_by_default(monkeypatch):
    monkeypatch.delenv("CONTROL_PLANE_TOKEN_ROTATION_ENABLED", raising=False)
    configuration = _make_configuration()
    client.Configuration.set_default(configuration)

    _enable_external_token_rotation("sandbox")

    live = client.Configuration.get_default()
    assert isinstance(live.refresh_api_key_hook, RotatingServiceAccountToken)


async def test_enable_external_token_rotation_respects_opt_out(monkeypatch):
    monkeypatch.setenv("CONTROL_PLANE_TOKEN_ROTATION_ENABLED", "false")
    configuration = _make_configuration()
    configuration.refresh_api_key_hook = None
    client.Configuration.set_default(configuration)

    _enable_external_token_rotation("sandbox")

    live = client.Configuration.get_default()
    assert live.refresh_api_key_hook is None


async def test_expiration_seconds_floored_to_kubernetes_minimum():
    hook = RotatingServiceAccountToken(
        service_account_name="boxkite-control-plane", namespace="sandbox", expiration_seconds=60
    )
    assert hook._expiration_seconds == 600


async def test_mint_call_is_bounded_by_a_timeout(monkeypatch):
    """A hung TokenRequest call must not block forever -- it's awaited by
    every outgoing K8s request in the process via the refresh hook."""
    configuration = _make_configuration(initial_token="still-the-old-token")
    hook = RotatingServiceAccountToken(
        service_account_name="boxkite-control-plane", namespace="sandbox", expiration_seconds=3600
    )
    monkeypatch.setattr(RotatingServiceAccountToken, "_MINT_TIMEOUT_SECONDS", 0.05)

    async def hanging_create_token(self, **kwargs):
        await asyncio.sleep(5.0)  # much longer than the monkeypatched timeout
        return _fake_token_request_result("should-never-arrive", timedelta(hours=1))

    monkeypatch.setattr(
        client.CoreV1Api, "create_namespaced_service_account_token", hanging_create_token
    )

    await asyncio.wait_for(hook(configuration), timeout=2.0)  # must return promptly, not hang

    assert configuration.api_key["BearerToken"] == "still-the-old-token"
    assert hook._last_failure_at is not None


async def test_repeated_calls_during_failure_cooldown_do_not_re_attempt_the_mint(monkeypatch):
    """After a failed mint, concurrent/subsequent callers within the
    cooldown window must not each attempt their own mint -- that would
    serialize a pile-up of failing (and, absent the timeout fix, possibly
    slow) calls for as long as an outage lasts."""
    configuration = _make_configuration(initial_token="still-the-old-token")
    hook = RotatingServiceAccountToken(
        service_account_name="boxkite-control-plane", namespace="sandbox", expiration_seconds=3600
    )

    call_count = 0

    async def failing_create_token(self, **kwargs):
        nonlocal call_count
        call_count += 1
        raise RuntimeError("simulated API server failure")

    monkeypatch.setattr(
        client.CoreV1Api, "create_namespaced_service_account_token", failing_create_token
    )

    await hook(configuration)
    assert call_count == 1

    # Still within the cooldown window -- must not attempt another mint.
    await hook(configuration)
    await hook(configuration)
    assert call_count == 1


async def test_mint_retries_again_after_the_cooldown_window_elapses(monkeypatch):
    configuration = _make_configuration(initial_token="still-the-old-token")
    hook = RotatingServiceAccountToken(
        service_account_name="boxkite-control-plane", namespace="sandbox", expiration_seconds=3600
    )

    call_count = 0

    async def create_token(self, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise RuntimeError("simulated API server failure")
        return _fake_token_request_result("recovered-token", timedelta(hours=1))

    monkeypatch.setattr(client.CoreV1Api, "create_namespaced_service_account_token", create_token)

    await hook(configuration)
    assert call_count == 1
    assert configuration.api_key["BearerToken"] == "still-the-old-token"

    # Simulate the cooldown window having fully elapsed.
    hook._last_failure_at = datetime.now(timezone.utc) - hook._FAILURE_COOLDOWN - timedelta(seconds=1)

    await hook(configuration)
    assert call_count == 2
    assert configuration.api_key["BearerToken"] == "recovered-token"
    assert hook._last_failure_at is None  # reset on success
