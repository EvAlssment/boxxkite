"""Tests for Settings.DATABASE_URL's bare-scheme-to-asyncpg normalization
(config.py). Managed Postgres providers (Render, Heroku, Railway, ...) hand
back a connection string with a bare `postgresql://`/`postgres://` scheme --
create_async_engine() (db.py) requires the `+asyncpg` driver suffix and
raises immediately without it, so this normalization is required for any of
those providers' connection-string env var to work at all (see
deploy/render.yaml's DATABASE_URL for the concrete case this fixes).
"""

from __future__ import annotations

from control_plane.config import Settings


def test_bare_postgresql_scheme_gets_asyncpg_driver_appended() -> None:
    settings = Settings(
        DATABASE_URL="postgresql://user:pass@host:5432/boxkite_control_plane"
    )
    assert settings.DATABASE_URL == (
        "postgresql+asyncpg://user:pass@host:5432/boxkite_control_plane"
    )


def test_bare_postgres_scheme_gets_asyncpg_driver_appended() -> None:
    """`postgres://` (no trailing "ql") is the legacy scheme some providers
    (Heroku historically) still hand back."""
    settings = Settings(
        DATABASE_URL="postgres://user:pass@host:5432/boxkite_control_plane"
    )
    assert settings.DATABASE_URL == (
        "postgresql+asyncpg://user:pass@host:5432/boxkite_control_plane"
    )


def test_already_correct_asyncpg_url_is_left_untouched() -> None:
    url = "postgresql+asyncpg://user:pass@host:5432/boxkite_control_plane"
    settings = Settings(DATABASE_URL=url)
    assert settings.DATABASE_URL == url


def test_sqlite_default_is_left_untouched() -> None:
    settings = Settings(DATABASE_URL="sqlite+aiosqlite:///./control_plane.db")
    assert settings.DATABASE_URL == "sqlite+aiosqlite:///./control_plane.db"


def test_non_default_postgres_driver_is_left_untouched() -> None:
    """A DSN that already names a different driver (e.g. psycopg, for a
    deployment intentionally not using asyncpg) must not be rewritten."""
    url = "postgresql+psycopg://user:pass@host:5432/boxkite_control_plane"
    settings = Settings(DATABASE_URL=url)
    assert settings.DATABASE_URL == url
