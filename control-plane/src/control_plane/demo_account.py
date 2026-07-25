"""The well-known internal "demo" Account backing the public playground
(issue #103, routers/demo_playground.py).

A single, fixed-email Account row -- no password, no API keys, never
reachable via `/v1/auth/login` (a social-login-only account's
`password_hash is None` already produces the "no_password_set" error there)
-- shared by every anonymous demo-playground visitor, purely so
`UsagePolicy`/`SandboxSessionRepository`'s existing account-scoped
bookkeeping machinery (create_session, destroy_session, count_active_for_
account, ...) works completely unmodified for demo traffic too.

Kept in its own tiny module rather than inside routers/demo_playground.py:
reaper.py (a background task module, not a router) also needs this exact
identity to reap demo sessions on their own much shorter
BOXKITE_DEMO_LIFETIME_MINUTES cutoff instead of the far longer
BOXKITE_MAX_SESSION_MINUTES every real account gets -- importing a router
module from a background-task module would invert this codebase's normal
dependency direction (routers depend on core modules, not vice versa).
"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from .models_orm import Account
from .repository import AccountRepository

DEMO_ACCOUNT_EMAIL = "demo-playground@boxkite.internal"


async def get_or_create_demo_account(db: AsyncSession) -> Account:
    """Fetch the demo account, creating it on first use.

    A race between two concurrent first-ever demo requests (each seeing no
    existing row and trying to insert one) is resolved by catching the
    unique-email IntegrityError the loser gets and re-fetching the winner's
    row, rather than failing the request -- the same pattern
    rate_limit.py's PostgresRateLimiter already uses for its own insert-vs-
    concurrent-insert race.
    """
    accounts = AccountRepository(db)
    account = await accounts.get_by_email(DEMO_ACCOUNT_EMAIL)
    if account is not None:
        return account

    account = Account(email=DEMO_ACCOUNT_EMAIL, password_hash=None)
    db.add(account)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        account = await accounts.get_by_email(DEMO_ACCOUNT_EMAIL)
        if account is None:
            raise
        return account
    await db.refresh(account)
    return account
