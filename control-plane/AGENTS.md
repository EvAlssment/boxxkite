# AGENTS.md — control-plane

Guide for an AI coding agent working in `control-plane/`. For DCO
sign-off and the fork/PR flow, see the repo root's
[CONTRIBUTING.md](../CONTRIBUTING.md).

## What this is

A fully separate Python package (own `pyproject.toml`, own
`src/control_plane/`, own `tests/`) implementing the hosted, multi-tenant
FastAPI control-plane. Installing the root `boxxkite` package does **not**
set this up.

## Setup

From the repo root, with the root `.venv` already active:

```bash
pip install -e .                  # root package, as a local sibling dependency
cd control-plane
pip install -e ".[dev]"
```

## Test

```bash
pytest tests/
```

Runs against `aiosqlite`, not real Postgres — no external database needed.
The `snapshots` extra (`boto3`/`azure-storage-blob`/`azure-identity`) is
only needed to exercise a real cloud storage client; normal tests use an
in-memory fake (`conftest.py`'s `FakeSnapshotStorageClient`).
`test_create_session_race_postgres.py` reads a real Postgres URL from the
environment and is explicitly an infra-level check, not part of a normal
test pass — don't expect it to run (or need to run it) locally.

## Schema changes

Every model lives in `src/control_plane/models_orm.py` — it's the actual
source of truth; its own module docstring is an index, not exhaustive, so
treat the class definitions themselves as authoritative for any table not
mentioned there.

A schema change needs an Alembic migration in `migrations/versions/`.
Check `alembic upgrade head` chains cleanly from the current head
(`down_revision` must point at the actual current head, not `main`'s
latest merged migration if you branched before it) — this repo has hit
migration-chain drift before. Match the existing migrations' style: plain
`op.add_column`/`op.drop_column`, no idempotency guards, sequential
revision ids. Tests use `Base.metadata.create_all()` directly from the
ORM models (not the migration chain), so a schema mismatch between your
model change and your migration won't be caught by `pytest tests/` alone
— verify the migration separately:

```bash
DATABASE_URL="sqlite+aiosqlite:////tmp/verify.db" python -m alembic upgrade head
```

## Router/service conventions worth knowing before you touch them

- Every DB lookup is scoped by `account_id` at the query layer (see any
  `*Repository.get_for_account`), never filtered after the fact — a
  cross-tenant lookup 404s, identically to a nonexistent id, so a caller
  can't even probe existence. Match this pattern for any new
  account-scoped resource.
- `routers/snapshots.py` imports several "private" (underscore-prefixed)
  helpers directly from `routers/sandboxes.py`
  (`_get_active_session_or_404`, `_resolve_image_ref_or_404`, etc.) —
  reusing a sibling router's helper this way is the established
  convention here, not a smell to fix.
- `UsagePolicy.create_session` is the single call site for the
  `sandbox.created`/`sandbox.destroyed` webhook events, deliberately not
  `routers/sandboxes.py` — this covers both a normal API-triggered create
  and the background reaper's teardown with one hook, not two. Don't
  duplicate webhook-firing logic in a router.
- `LimitExceededError` messages must never mention a dollar amount or a
  plan/tier name (see `test_usage_limits.py`'s `_assert_no_pricing_language`
  helper, reused across several test files) — this control-plane's error
  copy is deliberately pricing-agnostic.
