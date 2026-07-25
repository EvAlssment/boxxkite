# boxkite-client (TypeScript/JavaScript)

[![npm](https://img.shields.io/npm/v/boxkite-client?label=npm)](https://www.npmjs.com/package/boxkite-client)

A TypeScript/JavaScript client for a **hosted** boxkite control-plane —
create sandboxes, run commands, edit files, over HTTP. Works in Node (18+)
and the browser, and mirrors the Python SDK (`boxkite-client` on PyPI,
method names in camelCase).

## Install

```bash
npm install boxkite-client
```

## Quickstart

```typescript
import { BoxkiteClient } from "boxkite-client";

const client = new BoxkiteClient({
  baseUrl: "https://your-control-plane.example.com",
  apiKey: "bxk_live_...",
});

const result = await client.withSandbox(async (sb) => {
  const exec = await sb.exec("python3 -c 'print(1 + 1)'");
  await sb.fileCreate("notes.txt", "hello from boxkite-client\n");
  const viewed = await sb.view("notes.txt");
  return { exec, viewed };
});
// sandbox is destroyed automatically here, even if the callback threw
```

> **Never put a real `apiKey` in code that ships to a browser.** It's a
> full-privilege, long-lived credential, visible in devtools to anyone
> visiting the page. If a browser app needs to call boxkite, mint a
> short-lived, scoped credential from your own backend instead.

Also available: file/directory search (`ls`/`glob`/`grep`), long-running
background processes (`startProcess`/`getProcessOutput`/`stopProcess`),
signed preview URLs for exposing a port, an audit-log feed
(`getLog`/`watch`), interactive human takeover over a raw WebSocket,
desktop (GUI) takeover over the same raw-WebSocket pattern, secret
management (`createSecret`/`listSecrets`/`deleteSecret`, for use via
`createSandbox({ secretNames: [...] })`), and
`createSandboxTools()` factories for LangChain.js (`boxkite-client/langchain`)
and the Vercel AI SDK (`boxkite-client/vercel-ai`). Full reference with
examples for all of these:
[`docs/API.md`](https://github.com/EvAlssment/boxkite/blob/main/docs/API.md).

## Error handling

```typescript
import { BoxkiteApiError } from "boxkite-client";

try {
  await client.exec(sandbox.id, "echo hi");
} catch (err) {
  if (err instanceof BoxkiteApiError && err.code === "concurrent_sandbox_limit_reached") {
    // back off, destroy an old session, etc.
  }
}
```

A network-level failure (DNS, TLS, timeout) throws `BoxkiteConnectionError` instead.

## Automatic retries

Retries are off by default. Pass a `retry` option to enable exponential
backoff with jitter for transient failures — connection errors, HTTP `429`,
and `5xx` — on idempotent requests only (`GET`/`PUT`/`DELETE`), so a
non-idempotent `POST` like `createSandbox`/`exec` is never silently
replayed. A server `Retry-After` header is honored when present.

```typescript
const client = new BoxkiteClient({
  baseUrl: "https://your-control-plane.example.com",
  apiKey: "bxk_live_...",
  retry: {}, // sensible defaults: maxRetries 2, 500ms base, 30s cap, factor 2
});

// Or tune individual knobs:
new BoxkiteClient({
  baseUrl,
  apiKey,
  retry: { maxRetries: 4, initialDelayMs: 250, maxDelayMs: 10_000, respectRetryAfter: true },
});
```

## Development

```bash
npm install
npm test   # builds with tsc, then runs node's built-in test runner against dist/
```

## Related tools

Moving an in-progress local Claude Code/Codex CLI/opencode session (full
conversation history, not just a diff) into a fresh boxkite sandbox is
handled by the separate `boxkite-handoff` CLI (Python, built on
`sdk-python`, not this SDK) — see
[`../docs/handoff-adapters.md`](../docs/handoff-adapters.md) and
[`../handoff-cli/README.md`](../handoff-cli/README.md). Not yet published
to PyPI.

See the [root README](https://github.com/EvAlssment/boxkite#readme) for
what boxkite is and the full self-hosting story.
