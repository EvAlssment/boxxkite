# AGENTS.md — sdk-go

Guide for an AI coding agent working in `sdk-go/`. For DCO sign-off and
the fork/PR flow, see the repo root's [CONTRIBUTING.md](../CONTRIBUTING.md).

## Setup

No explicit install step — Go modules resolve via `go.mod`/`go.sum` on
first build:

```bash
cd sdk-go
go build ./...
```

## Test

```bash
go test ./... -race -cover
```

Fakes the control-plane with `net/http/httptest`. `Takeover`'s WebSocket
tests spin up a real `gorilla/websocket` upgrader against an
`httptest.Server` — there's no mock-WebSocket layer, so a change to the
takeover protocol needs a real round-trip test, not a mocked one.

## Lint

```bash
go vet ./...
```

No `.golangci.yml` — `go vet` is the only static check this package runs.

## Conventions

Hand-written against the control-plane's REST API, same posture as
`sdk-python`/`sdk-js`/`sdk-rust` (see `sdk-python/AGENTS.md`'s note on
cross-SDK drift) — no codegen, so a control-plane API change needs a
manual matching edit here. Resource-per-file layout (one `.go` file per
API resource) — match that when adding a new resource wrapper rather than
growing an existing file or introducing a different organization.
