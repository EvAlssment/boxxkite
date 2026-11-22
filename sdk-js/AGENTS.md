# AGENTS.md — sdk-js

Guide for an AI coding agent working in `sdk-js/`. For DCO sign-off and
the fork/PR flow, see the repo root's [CONTRIBUTING.md](../CONTRIBUTING.md).

## Setup

```bash
cd sdk-js
bun install
```

## Test

```bash
bun run fmt:check   # oxfmt
bun run lint        # oxlint
bun run check       # bun check: type errors, same as tsc
bun run test        # builds with tsc (TypeScript 7), then node --test on dist/
```

Tests run under Node, not bun: the SDK ships for Node, so they run on the
runtime users have. They mock the control-plane; no real deployment
needed. `bun run fmt` rewrites files to the house format.

## Conventions

- oxlint runs every rule category except nursery. `.oxlintrc.json`
  switches off, by name, only the rules this code broke when it was
  turned on; remove a rule from that list once the code passes it.
- Hand-written against the control-plane's REST API, same posture as
  `sdk-python`/`sdk-go`/`sdk-rust` (see `sdk-python/AGENTS.md`'s note on
  cross-SDK drift) — no codegen, so a control-plane API change needs a
  manual matching edit here.
