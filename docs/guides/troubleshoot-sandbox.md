# Troubleshoot a sandbox with Claude Code or Cursor

The `troubleshoot-sandbox` skill gives a coding agent a read-only workflow for
investigating a hosted boxxkite session. It uses the diagnostics surface that
is already available from the CLI and control-plane:

- `boxxkite diagnostics summary <SESSION_ID> --json`
- `boxxkite diagnostics inspect <SESSION_ID> --json`
- `boxxkite diagnostics logs <SESSION_ID> --json`
- `boxxkite diagnostics events <SESSION_ID> --json`

The commands inspect a session owned by the configured account. They return
the server's `why`, `error_code`, retryability, remediation, runtime, pod and
container state, logs, events, and audit entries where available. They do not
repair, recreate, or change a sandbox.

## Install into a project

From the project where the coding agent runs:

```bash
boxxkite skills install claude-code
# or
boxxkite skills install cursor
```

The installer writes one project-local file:

| Target | File |
| --- | --- |
| Claude Code | `.claude/skills/troubleshoot-sandbox/SKILL.md` |
| Cursor | `.cursor/rules/troubleshoot-sandbox.mdc` |

Existing files are never overwritten unless `--force` is passed. The
installer writes only the skill text; it does not read or copy the boxxkite
API key, control-plane configuration, or any other credential.

The Claude Code source artifact is published under
[`skills/troubleshoot-sandbox/`](../../skills/troubleshoot-sandbox/), and the
Cursor variant is under [`.cursor/rules/`](../../.cursor/rules/). The installed
files are project-local so the agent can load them in the project where it is
working.

## Use it safely

The skill is for hosted control-plane sessions. A local `boxxkite up` stack has
one Docker Compose sidecar and no per-session diagnostics API. If no session ID
is supplied, the agent may use `boxxkite session ls --active-only` only when it
returns exactly one active session; otherwise it must ask which session to
inspect.

The skill tells the agent to start with `summary`, then fetch `inspect`, `logs`,
or `events` only when needed. It treats `unavailable` as a real diagnostic
result and does not turn an empty log or event list into a health claim. API
keys must stay in the user's existing CLI/API-key handling and out of prompts,
logs, and generated files.

For the HTTP equivalent, the four routes are:

```text
GET /v1/sandboxes/<SESSION_ID>/diagnostics/summary
GET /v1/sandboxes/<SESSION_ID>/diagnostics/inspect
GET /v1/sandboxes/<SESSION_ID>/diagnostics/logs
GET /v1/sandboxes/<SESSION_ID>/diagnostics/events
```

These routes require the normal account-scoped API-key authorization. Use the
CLI unless an existing integration already handles that authorization safely.
