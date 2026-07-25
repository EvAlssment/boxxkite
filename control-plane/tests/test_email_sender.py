"""Unit tests for email_sender.py's default LoggingEmailSender -- the
non-production placeholder used until a deployment wires in a real
EmailSender (issue #79)."""

from __future__ import annotations

import logging

import pytest

from control_plane.config import settings
from control_plane.email_sender import (
    LoggingEmailSender,
    get_email_sender,
    reset_email_sender_for_tests,
    set_email_sender_for_production,
)


@pytest.fixture(autouse=True)
def _reset_singleton():
    reset_email_sender_for_tests()
    yield
    reset_email_sender_for_tests()


def test_logging_email_sender_is_not_production_ready():
    assert LoggingEmailSender.is_production_ready is False


async def test_logging_email_sender_logs_token_in_dev_environment(monkeypatch, caplog):
    monkeypatch.setattr(settings, "ENVIRONMENT", "development")
    sender = LoggingEmailSender()

    with caplog.at_level(logging.WARNING):
        await sender.send_password_reset_email(to_email="dev@example.com", reset_token="raw-token-123")

    assert any("raw-token-123" in record.getMessage() for record in caplog.records)


async def test_logging_email_sender_does_not_log_token_outside_dev_environment(monkeypatch, caplog):
    monkeypatch.setattr(settings, "ENVIRONMENT", "production")
    sender = LoggingEmailSender()

    with caplog.at_level(logging.WARNING):
        await sender.send_verification_email(to_email="prod@example.com", verification_token="super-secret-token")

    for record in caplog.records:
        assert "super-secret-token" not in record.getMessage()


def test_get_email_sender_returns_singleton():
    first = get_email_sender()
    second = get_email_sender()
    assert first is second


def test_set_email_sender_for_production_overrides_default():
    class _FakeProdSender:
        is_production_ready = True

        async def send_password_reset_email(self, *, to_email: str, reset_token: str) -> None:
            pass

        async def send_verification_email(self, *, to_email: str, verification_token: str) -> None:
            pass

    fake = _FakeProdSender()
    set_email_sender_for_production(fake)
    assert get_email_sender() is fake
