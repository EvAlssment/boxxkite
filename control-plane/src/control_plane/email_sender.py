"""Outbound transactional email for the opt-in auth flows added for issue
#79 (password reset, email verification).

This control plane has no email-sending capability today (no SMTP/SES/
SendGrid credential anywhere in config.py) -- per the task's explicit
scope, this module stubs delivery behind a clearly-named interface
(`EmailSender`) rather than fabricating a working transport. The token
generation/validation/password-update logic in routers/auth.py is real and
fully covered by tests; only the "send an email" step is a placeholder.

Follows the same environment-dependent-backend shape already established
by `storage_client.py`/`secrets_kms.py`: a `Protocol` plus a real
implementation a deployment can plug in later, resolved through a lazily-
initialized, test-overridable factory (`get_email_sender`, overridden via
`app.dependency_overrides[get_email_sender_dep]` in tests -- see
deps.py). The default, `LoggingEmailSender`, is a clearly-marked
**non-production** placeholder: it logs that an email *would* be sent and,
in dev/test environments only, logs the raw token too (so a developer can
exercise the full reset/verify flow locally without a real mail transport).
It never actually delivers anything. A production deployment that enables
`BOXKITE_PASSWORD_RESET_ENABLED` or `BOXKITE_EMAIL_VERIFICATION_ENABLED`
MUST supply a real `EmailSender` implementation (SMTP, SES, SendGrid, ...)
via `set_email_sender_for_production` (or by overriding `get_email_sender`
in a deployment-specific entrypoint) -- the same "insecure-dev-default,
must be swapped for production" posture as `JWT_SECRET` and
`LocalDevSecretsKmsClient`, and just as loudly disclosed here.
"""

from __future__ import annotations

import logging
from typing import Protocol

from .config import settings

logger = logging.getLogger(__name__)


class EmailSender(Protocol):
    """Two methods, one per opt-in flow. Both are best-effort from the
    caller's perspective -- routers/auth.py never lets an email-delivery
    failure turn into a 500, and never lets it reveal whether an account
    exists (see `_send_password_reset_email_best_effort`)."""

    async def send_password_reset_email(self, *, to_email: str, reset_token: str) -> None: ...

    async def send_verification_email(self, *, to_email: str, verification_token: str) -> None: ...


class LoggingEmailSender:
    """Non-production placeholder. Logs that an email would have been
    sent; never actually sends one. Raw tokens are only ever logged when
    `settings.is_dev_environment` is true, specifically so a local
    developer can copy the token out of the log to exercise the flow --
    the same trade-off `secrets_kms.py`'s `LocalDevSecretsKmsClient` makes
    for its own dev-only key material.
    """

    is_production_ready = False

    def __init__(self) -> None:
        logger.warning(
            "[control-plane] Using LoggingEmailSender -- no email is actually being "
            "sent. This is a non-production placeholder; a real deployment must "
            "supply an EmailSender implementation (SMTP/SES/SendGrid/...) before "
            "relying on password-reset or email-verification delivery."
        )

    async def send_password_reset_email(self, *, to_email: str, reset_token: str) -> None:
        if settings.is_dev_environment:
            logger.warning(
                "[control-plane][dev-only] Password reset requested for %s -- token: %s",
                to_email,
                reset_token,
            )
        else:
            logger.warning(
                "[control-plane] Password reset requested for %s, but no real EmailSender "
                "is configured -- the email was NOT sent.",
                to_email,
            )

    async def send_verification_email(self, *, to_email: str, verification_token: str) -> None:
        if settings.is_dev_environment:
            logger.warning(
                "[control-plane][dev-only] Email verification requested for %s -- token: %s",
                to_email,
                verification_token,
            )
        else:
            logger.warning(
                "[control-plane] Email verification requested for %s, but no real EmailSender "
                "is configured -- the email was NOT sent.",
                to_email,
            )


_email_sender: EmailSender | None = None


def get_email_sender() -> EmailSender:
    """Overridable in tests via `app.dependency_overrides[get_email_sender_dep]`
    (see deps.py) or in a production entrypoint via
    `set_email_sender_for_production`."""
    global _email_sender
    if _email_sender is None:
        _email_sender = LoggingEmailSender()
    return _email_sender


def set_email_sender_for_production(sender: EmailSender) -> None:
    """Called by a deployment-specific entrypoint (not this repo's default
    `main.py`, which has no real mail transport to wire up) to replace the
    default `LoggingEmailSender` with a real implementation before serving
    traffic."""
    global _email_sender
    _email_sender = sender


def reset_email_sender_for_tests() -> None:
    global _email_sender
    _email_sender = None
