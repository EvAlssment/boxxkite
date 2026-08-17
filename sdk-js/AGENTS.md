# AGENTS.md — sdk-js

Guide for an AI coding agent working in `sdk-js/`. For DCO sign-off and
the fork/PR flow, see the repo root's [CONTRIBUTING.md](../CONTRIBUTING.md).

## Setup

```bash
cd sdk-js
npm install
```

## Test

```bash
npm test
```

Builds via `tsc`, then runs the compiled output through Node's built-in
test runner (`node --test`) — there's no separate build step to run
first, `npm test` does both. Tests mock the control-plane; no real
deployment needed.

## Conventions

- No lint step is configured for this package (no `eslintrc`/
  `eslint.config.*`) — `tsc`'s own type-checking during the build is the
  only static check `npm test` runs. Don't assume an ESLint pass is part
  of this package's own checks; the root's combined `ruff check` command
  doesn't cover JS/TS either.
- Hand-written against the control-plane's REST API, same posture as
  `sdk-python`/`sdk-go`/`sdk-rust` (see `sdk-python/AGENTS.md`'s note on
  cross-SDK drift) — no codegen, so a control-plane API change needs a
  manual matching edit here.
