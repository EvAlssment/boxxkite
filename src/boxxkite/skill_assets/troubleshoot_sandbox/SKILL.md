---
name: troubleshoot-sandbox
description: Diagnose a hosted boxxkite sandbox with its account-scoped diagnostics commands. Use when a sandbox is stuck, stops unexpectedly, cannot start, or behaves differently from expected.
---

# Troubleshoot a boxxkite sandbox

Use this workflow for a hosted control-plane session. It is read-only: the
diagnostics commands fetch status, pod/container state, recent container logs,
Kubernetes lifecycle events, and the sandbox audit entries already exposed by
boxxkite.

Do not use this workflow to claim that a local `boxxkite up` stack has a
session diagnostics API. Local Docker Compose mode has one sidecar and no
per-session control-plane diagnostics surface.

## Identify the session

Use a session ID supplied by the user when there is one. Otherwise run:

```text
boxxkite session ls --active-only
```

If exactly one active session is listed, use its ID. If there are zero or
multiple active sessions, ask the user which ID to inspect. Never guess from a
label or from the order of an unfiltered list. A destroyed session may still
be inspected when its ID is known.

## Collect diagnostics

Start with the summary:

```text
boxxkite diagnostics summary <SESSION_ID> --json
```

Record `status`, `why`, `error_code`, `retryable`, `remediation`, `runtime`,
and `unavailable`. The response may also include pod and container state,
recent logs, events, and audit entries.

Drill down only as needed:

```text
boxxkite diagnostics inspect <SESSION_ID> --json
boxxkite diagnostics logs <SESSION_ID> --json
boxxkite diagnostics events <SESSION_ID> --json
```

These commands map to the existing account-scoped HTTP routes:

```text
GET /v1/sandboxes/<SESSION_ID>/diagnostics/summary
GET /v1/sandboxes/<SESSION_ID>/diagnostics/inspect
GET /v1/sandboxes/<SESSION_ID>/diagnostics/logs
GET /v1/sandboxes/<SESSION_ID>/diagnostics/events
```

If using HTTP directly, send the request through the configured control-plane
with the user's existing API-key handling. Do not ask the user to paste an API
key into the conversation, print it, or copy it into a file. Preserve the
account-scoped authorization and use HTTPS except for explicitly local
development.

## Interpret the result

Treat `why`, `error_code`, `retryable`, `remediation`, and `unavailable` as the
authoritative server result. Do not invent a cause when a field is missing or
diagnostics are unavailable.

- `killed: exceeded memory limit`: correlate the container exit code/reason,
  logs, and events. The sandbox was killed for memory pressure; reduce the
  workload or ask the operator about the deployment's resource policy.
- `cannot start: container image could not be pulled`: inspect image-pull
  events and their messages. Check the configured image reference and the
  cluster's registry access; do not suggest changing credentials blindly.
- `restarting: container is crash-looping`: inspect the failing container's
  reason, message, restart count, and logs before retrying.
- `evicted: node resource pressure forced the sandbox to stop`: report the
  eviction and the event message; this requires operator-side node capacity
  investigation.
- `denied: network egress is blocked by the default-deny policy`: report the
  destination and policy evidence if present. Do not advise weakening the
  sandbox network policy as a first response.
- `waiting: pod has not been scheduled`: inspect events for the scheduler's
  concrete reason and wait or escalate according to the returned
  `retryable`/`remediation` fields.
- `running normally` or `completed normally`: distinguish a runtime issue from
  an application or agent issue using the logs and audit entries.
- An `unavailable` message: report it explicitly. An empty log or event list
  is not proof that the pod was healthy.

## Report back

Return the session ID, status, `why`, `error_code`, retryability, server
remediation, runtime, and the smallest evidence-backed next step. Separate
what the diagnostics prove from what remains unknown. Do not destroy, recreate,
change permissions, change egress, or alter credentials unless the user
explicitly asks for that separate operation.
