# boxkite (Go client)

[![Go Reference](https://pkg.go.dev/badge/github.com/EvAlssment/boxkite/sdk-go.svg)](https://pkg.go.dev/github.com/EvAlssment/boxkite/sdk-go)

A Go client for a **hosted** boxkite control-plane — create sandboxes, run
commands, edit files, over HTTP. Mirrors `sdk-python`'s `BoxkiteClient`
(PyPI: `boxkite-client`) and `sdk-js`'s `BoxkiteClient` (npm:
`boxkite-client`) method-for-method, adapted to Go idiom (`(result, err)`
returns, a request-struct pattern instead of keyword arguments, `context.Context`
on every call). Not a client for the `boxkite` package itself
(`SandboxManager`, embedded directly against a Kubernetes cluster) — use
this to talk to *someone else's* running control-plane, hosted or
self-hosted, over its API.

## Install

```bash
go get github.com/EvAlssment/boxkite/sdk-go
```

## Quickstart

```go
package main

import (
	"context"
	"fmt"
	"log"

	"github.com/EvAlssment/boxkite/sdk-go"
)

func main() {
	client, err := boxkite.NewClient("https://your-control-plane.example.com", "bxk_live_...")
	if err != nil {
		log.Fatal(err)
	}

	ctx := context.Background()
	err = client.WithSandbox(ctx, boxkite.CreateSandboxRequest{Label: boxkite.Ptr("demo")}, func(sb *boxkite.Session) error {
		result, err := sb.Exec(ctx, "python3 -c 'print(1 + 1)'", nil)
		if err != nil {
			return err
		}
		fmt.Println(result.Stdout) // "2\n"

		if _, err := sb.FileCreate(ctx, "notes.txt", "hello from boxkite\n", nil); err != nil {
			return err
		}
		viewed, err := sb.View(ctx, "notes.txt", nil)
		if err != nil {
			return err
		}
		fmt.Println(viewed.Content)
		return nil
	})
	// sandbox is destroyed automatically here, even if the callback returned an error
	if err != nil {
		log.Fatal(err)
	}
}
```

`WithSandbox` is the create-on-enter, destroy-on-exit convenience mirroring
`sdk-python`'s `with client.sandbox() as sb:` context manager / `sdk-js`'s
`withSandbox`. Prefer `Client.CreateSandbox` / `Client.DestroySandbox`
directly if you want the sandbox to outlive the call that created it.

Every optional parameter uses a `*Request`/`*Options` struct with
pointer-typed optional fields (`nil` == omit) rather than Go's nonexistent
keyword arguments — use `boxkite.Ptr(v)` to fill one in inline, e.g.
`boxkite.CreateSandboxRequest{Size: boxkite.Ptr("medium")}`.

Also available: file/directory search (`Ls`/`Glob`/`Grep`), long-running
background processes (`StartProcess`/`GetProcessOutput`/`StopProcess`),
signed preview URLs for exposing a port (`CreatePreviewURL`/
`RevokePreviewURL`), an audit-log feed (`GetLog`/`Watch`), interactive
human takeover over a raw WebSocket (`Takeover`), desktop (GUI) takeover
over the same raw-WebSocket pattern (`DesktopTakeover`), and CRUD for images,
volumes, webhooks, outbound-MCP connections, and secrets (`CreateSecret`/
`ListSecrets`/`DeleteSecret`). Full reference with examples for all of
these: [`docs/API.md`](https://github.com/EvAlssment/boxkite/blob/main/docs/API.md).

## Webhooks

Register a subscription with `CreateWebhook`, then verify each delivery's
`X-Boxkite-Webhook-Signature` header (`docs/WEBHOOKS-DESIGN.md` §6) before
trusting its body:

```go
webhook, err := client.CreateWebhook(ctx, boxkite.CreateWebhookRequest{
	URL:         "https://example.com/boxkite-webhook",
	EventTypes:  []string{"sandbox.created", "sandbox.destroyed", "audit_log.entry"},
	Description: boxkite.Ptr("webhooks example"),
})
if err != nil {
	log.Fatal(err)
}
fmt.Println("Signing secret (shown once, save it now):", webhook.Secret)
```

`crypto/hmac.Equal` is already constant-time (unlike Python's or Node's HMAC
comparison, which need `hmac.compare_digest`/`crypto.timingSafeEqual` called
out explicitly, Go's standard library gives you this for free) — a receiver
verifying deliveries can lean on it directly:

```go
func VerifySignature(secret, signatureHeader string, rawBody []byte, tolerance time.Duration) bool {
	var timestamp int64
	var signature string
	for _, part := range strings.Split(signatureHeader, ",") {
		key, value, ok := strings.Cut(part, "=")
		if !ok {
			continue
		}
		switch key {
		case "t":
			timestamp, _ = strconv.ParseInt(value, 10, 64)
		case "v1":
			signature = value
		}
	}

	if time.Since(time.Unix(timestamp, 0)).Abs() > tolerance {
		return false
	}

	mac := hmac.New(sha256.New, []byte(secret))
	mac.Write([]byte(strconv.FormatInt(timestamp, 10) + "."))
	mac.Write(rawBody)
	expected := hex.EncodeToString(mac.Sum(nil))

	return hmac.Equal([]byte(expected), []byte(signature))
}
```

Delete a subscription with `client.DeleteWebhook(ctx, webhook.ID)` when it's
no longer needed. A runnable end-to-end version of this flow (create, list,
verify a locally-signed payload, delete) lives in
[`examples/webhooks.go`](examples/webhooks.go):

```sh
BOXKITE_BASE_URL=https://your-control-plane BOXKITE_API_KEY=bxk_live_... \
    go run ./examples/webhooks.go
```

See `docs/WEBHOOKS-DESIGN.md` for the full event catalog, retry/backoff
schedule, and delivery-idempotency contract.

## Error handling

Every non-2xx response returns a `*boxkite.APIError` (`.StatusCode`,
`.Code`, `.Message`). A network-level failure (DNS, TLS, timeout,
connection refused) returns a `*boxkite.ConnectionError` instead. Both
implement `error`; neither panics.

```go
result, err := client.Exec(ctx, sandboxID, "echo hi", nil)
if err != nil {
	var apiErr *boxkite.APIError
	if errors.As(err, &apiErr) && apiErr.Code == "concurrent_sandbox_limit_reached" {
		// back off, destroy an old session, etc.
	}
	return err
}
```

## Retries

Automatic retry is **off by default**. Enable it with `WithRetry`, starting
from `DefaultRetryConfig()` (2 retries, exponential backoff with full
jitter, `Retry-After` honored):

```go
client, err := boxkite.NewClient(baseURL, apiKey, boxkite.WithRetry(boxkite.DefaultRetryConfig()))
```

Only idempotent verbs (`GET`/`HEAD`/`PUT`/`DELETE`/`OPTIONS`) are retried,
and only on a connection failure or a transient status (429, 500, 502, 503,
504) — a non-idempotent `POST` (create-sandbox/secret/webhook) is never
retried, so this can't double-create a resource. Backoff waits honor the
request's `context.Context`: a cancelled/expired `ctx` cuts the wait short
and returns immediately. Every field of `RetryConfig` is tunable if the
defaults don't fit.

## Streaming: `Watch` and `Takeover`

`Watch` returns a `*LogWatcher` you drive like `bufio.Scanner`:

```go
watcher, err := client.Watch(ctx, sandboxID)
if err != nil {
	log.Fatal(err)
}
defer watcher.Close()
for watcher.Next() {
	entry := watcher.Entry()
	fmt.Println(entry.Operation, entry.StartedAt)
}
if err := watcher.Err(); err != nil {
	log.Fatal(err)
}
```

`Takeover` opens `WS /v1/sandboxes/{id}/takeover` and returns a
`*websocket.Conn` ([`github.com/gorilla/websocket`](https://github.com/gorilla/websocket))
for raw byte bridging — there is no message envelope, exactly as described
in `docs/API.md`:

```go
conn, err := client.Takeover(ctx, sandboxID)
if err != nil {
	log.Fatal(err)
}
defer conn.Close()
_ = conn.WriteMessage(websocket.BinaryMessage, []byte("ls -la\n"))
_, reply, err := conn.ReadMessage()
```

Unlike `sdk-js`'s `takeover()` (constrained by the browser WebSocket API,
which cannot set a custom header), this authenticates with a normal
`Authorization: Bearer <apiKey>` header on the handshake — the same
approach `sdk-python`'s `takeover()` uses, since Go's `websocket.Dialer`,
like Python's `websockets` library, can set arbitrary headers on a
WebSocket upgrade request.

## Design notes / library choices

- **WebSocket client:** [`gorilla/websocket`](https://github.com/gorilla/websocket)
  (BSD-2-Clause, 24k+ GitHub stars, not archived) — the de facto standard
  for Go WebSocket clients and servers, with straightforward support for
  setting a custom `Authorization` header on `Dialer.DialContext`, which
  `Takeover` needs. [`coder/websocket`](https://github.com/coder/websocket)
  (formerly `nhooyr.io/websocket`) was also evaluated — a more modern,
  `context`-first API with more recent commits — but `gorilla/websocket`'s
  wider adoption and this SDK's modest WebSocket surface (one raw
  byte-bridging connection, no advanced framing) didn't justify picking
  the less-established option.
- **SSE (`Watch`):** hand-rolled with `bufio.Scanner` over the response
  body rather than a third-party SSE client library. `sdk-python` and
  `sdk-js` both hand-roll their own `data:`-line parser rather than pulling
  in a dependency for this too (see `_iter_sse_events`/`parseSseEvents` in
  their respective `client.py`/`client.ts`) — the actual parsing is ~20
  lines and this SDK's needs (decode one JSON object per event, respect
  `context.Context` cancellation, no `id:`/`event:` framing) don't need
  more than that. [`r3labs/sse`](https://github.com/r3labs/sse) was
  evaluated as an existing option and would work, but wasn't adopted, to
  stay consistent with the other two SDKs' own approach here.
- **Request shape:** every method with more than one or two optional
  parameters takes a `*Request`/`*Options` struct (pointer-typed optional
  fields) rather than Go's functional-options pattern
  (`WithLabel(...)`/`WithSize(...)`) — with ~15 methods each having 3-9
  optional fields, functional options would mean dozens of tiny `With*`
  constructors for marginal benefit over a plain struct literal. This is
  used consistently across the whole client (`CreateSandboxRequest`,
  `CreateImageRequest`, `ExecOptions`, `GrepOptions`, etc.) — the
  functional-options pattern is reserved for `Client` construction itself
  (`WithHTTPClient`, `WithTimeout`, `WithWebSocketDialer`, `WithRetry`),
  where there are few, rarely-combined options and the pattern's real
  strength (extensible construction without breaking existing call sites)
  applies.

## Explicitly out of scope for this pass

- **No LangChain-style tool-factory wrapper.** `sdk-python`'s
  `langchain_tools.py` / `sdk-js`'s LangChain.js and Vercel AI SDK tool
  factories have no direct Go analog here, because there is no dominant
  Go agent-framework tool-spec convention to mirror yet (no Go equivalent
  of LangChain/LangGraph/Vercel AI SDK has emerged as a clear standard at
  the time of writing). If one does, a `boxkite/langchain`-style subpackage
  wrapping `Client`'s methods into that framework's tool interface would
  be the natural next step.
- **No `payload_format`/`hec_token` fields on `CreateWebhookRequest`.**
  `control-plane/src/control_plane/schemas.py`'s `WebhookCreateRequest`
  supports a Splunk HEC delivery format (GitHub issue #125), but neither
  `sdk-python`'s nor `sdk-js`'s `create_webhook`/`createWebhook` expose it
  yet — same "match the existing SDKs' method sets" rationale as above.
- **No separate async/concurrent client variant.** Unlike
  `sdk-python`'s `AsyncBoxkiteClient` (a second class, because Python
  needs an explicit async/sync split), every method here already takes a
  `context.Context` and is safe to call from any goroutine — Go's
  goroutine-friendly blocking-call style doesn't need a parallel "async"
  API the way Python does.
- **No login/signup methods** (`POST /v1/auth/signup`/`/login`) — same
  scope as `sdk-python`/`sdk-js`: this client is constructed directly with
  an existing API key, not by authenticating a dashboard session itself.
  The opt-in dashboard-auth flows that *are* mirrored
  (`RequestPasswordReset`, `ConfirmPasswordReset`, `VerifyEmail`,
  `ResendVerification`, `RefreshToken`, `Logout`) match exactly what
  `sdk-python`'s `client.py` exposes.

## Development

```bash
go build ./...
go vet ./...
go test ./... -race -cover
```

Tests fake the control-plane with `net/http/httptest` (mirroring how
`sdk-python`'s tests use `httpx.MockTransport` and `sdk-js`'s use a fake
`fetch`) — no real deployment needed. WebSocket (`Takeover`) tests spin up
a real `gorilla/websocket` upgrader against an `httptest.Server`.

## Related tools

Moving an in-progress local Claude Code/Codex CLI/opencode session (full
conversation history, not just a diff) into a fresh boxkite sandbox is
handled by the separate `boxkite-handoff` CLI (Python, built on
`sdk-python`, not this SDK) — see
[`../docs/handoff-adapters.md`](../docs/handoff-adapters.md) and
[`../handoff-cli/README.md`](../handoff-cli/README.md). Not yet published
to PyPI.

See the [root README](https://github.com/EvAlssment/boxkite#readme) for
what boxkite is and the full self-hosting story, and
[`docs/API.md`](https://github.com/EvAlssment/boxkite/blob/main/docs/API.md)
for the complete REST API reference this client wraps.
