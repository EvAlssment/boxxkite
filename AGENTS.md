# AGENTS.md — root package

Guide for an AI coding agent (Claude Code, Cursor, etc.) working in this
package. For DCO sign-off, the fork/PR flow, and the combined lint/audit
command, see [CONTRIBUTING.md](CONTRIBUTING.md) — this file only covers
what's specific to the root `boxxkite` package.

## What lives here

The core sandbox library: `SandboxManager`, the handoff adapters
(`src/boxxkite/handoff/adapters/`), the CLI (`src/boxxkite/cli/`), and the
framework-agnostic tool surface (`src/boxxkite/tools/`). `control-plane/`,
`sdk-python/`, `sdk-js/`, `sdk-go/`, `sdk-rust/`, and `mcp-server/` are
separate packages with their own setup — see their own `AGENTS.md`.

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# Some tests import sidecar/main.py directly (fastapi/pydantic/aiofiles
# aren't declared in this package's own pyproject.toml, since they're the
# sidecar's dependencies, not boxxkite's):
pip install -r sidecar/requirements.txt
```

Cold install takes a few minutes, not seconds — the `dev` extra pulls in
`boxxkite-sandbox[langchain,llamaindex,openai-agents]`.

## Test

```bash
pytest tests/
```

Two markers matter: `pr` (a faster pre-merge subset) and `integration`
(slower, broader coverage) — check `pyproject.toml`'s `[tool.pytest.ini_options]`
before assuming a plain `pytest tests/` run covers everything a given
change needs.

## Before opening a PR

```bash
ruff check src/ tests/ control-plane/src/ control-plane/tests/
pip-audit --progress-spinner=off
```

`ruff`'s `select = ["F"]` here is deliberately correctness-only (unused
imports, undefined names, etc.), not a style linter — don't add style-only
fixes expecting them to be caught by this command.

## Handoff adapters specifically

If your change touches `src/boxxkite/handoff/adapters/`, read an existing
adapter's docstring first (`kimi.py` and `gemini.py` both document a real
live-verification record against the actual CLI they support) — the
established bar for this package is verifying claims about a tool's
session-storage format against that tool's real, installed binary, not
just its public docs. A plausible-looking path or field name that turns
out wrong is the single most common bug class across this package's
existing adapter PRs (#122, #123, #124's own follow-up commit all fixed
exactly this).

## Security-sensitive paths

Changes to `deploy/pod-template.yaml`, `deploy/network-policy.yaml`,
`sidecar/main.py`'s path/permission handling, or
`src/boxxkite/manager.py`'s security-context construction get extra
review scrutiny — see CONTRIBUTING.md's "Before you start" section.
