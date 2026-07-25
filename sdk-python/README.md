# boxkite-client

[![PyPI](https://img.shields.io/pypi/v/boxkite-client?label=PyPI)](https://pypi.org/project/boxkite-client/)

A Python client for a **hosted** boxkite control-plane — create sandboxes,
run commands, edit files, over HTTP. Not the boxkite package itself
(`boxkite-sandbox`, which embeds `SandboxManager` against your own
Kubernetes cluster) — use this to talk to *someone else's* running
control-plane, hosted or self-hosted, over its API.

## Install

```bash
pip install boxkite-client
pip install boxkite-client[langchain]  # for create_sandbox_tools
```

## Quickstart

```python
from boxkite_client import BoxkiteClient

client = BoxkiteClient(base_url="https://your-control-plane.example.com", api_key="bxk_live_...")

with client.sandbox(label="demo") as sb:
    result = sb.exec("python3 -c 'print(1 + 1)'")
    print(result["stdout"])  # "2\n"

    sb.file_create("notes.txt", "hello from boxkite-client\n")
    print(sb.view("notes.txt")["content"])
# sandbox is destroyed automatically here, even if an exception was raised above
```

Also available: `AsyncBoxkiteClient` (same shapes, `async`/`await`),
file/directory search (`ls`/`glob`/`grep`), long-running background
processes (`start_process`/`get_process_output`/`stop_process`), signed
preview URLs for exposing a port, an audit-log feed (`get_log`/`watch`),
interactive human takeover over a raw WebSocket, desktop (GUI) takeover
over the same raw-WebSocket pattern, secret management
(`create_secret`/`list_secrets`/`delete_secret`, for use via
`create_sandbox(secret_names=[...])`), and a `create_sandbox_tools()`
LangChain factory. Full reference with examples
for all of these: [`docs/API.md`](https://github.com/EvAlssment/boxkite/blob/main/docs/API.md).

## Error handling

Every non-2xx response raises `BoxkiteApiError` (`.status_code`, `.code`,
`.message`). A network-level failure raises `BoxkiteConnectionError`. Both
subclass `BoxkiteError`.

```python
from boxkite_client import BoxkiteApiError

try:
    client.exec(sandbox["id"], "echo hi")
except BoxkiteApiError as exc:
    if exc.code == "concurrent_sandbox_limit_reached":
        ...  # back off, destroy an old session, etc.
```

## Retries

Automatic retry is **off by default**. Pass a `RetryConfig` to enable it;
`RetryConfig()` carries sensible defaults (2 retries, exponential backoff
with full jitter, `Retry-After` honored):

```python
from boxkite_client import BoxkiteClient, RetryConfig

client = BoxkiteClient(base_url="...", api_key="...", retry=RetryConfig())
```

Only idempotent verbs (`GET`/`HEAD`/`PUT`/`DELETE`/`OPTIONS`) are retried,
and only on a connection failure or a transient status (429, 500, 502, 503,
504) — a non-idempotent `POST` (create-sandbox/secret/webhook) is never
retried, so this can't double-create a resource. `AsyncBoxkiteClient` takes
the same `retry=` argument and awaits its backoff. Every field of
`RetryConfig` is tunable if the defaults don't fit.

## Development

```bash
pip install -e ".[dev,langchain]"
pytest tests/
```

Tests mock the control-plane with `httpx.MockTransport` — no real deployment needed.

## Related tools

Moving an in-progress local Claude Code/Codex CLI/opencode session (full
conversation history, not just a diff) into a fresh boxkite sandbox is
handled by the separate `boxkite-handoff` CLI, built on this SDK — see
[`../docs/handoff-adapters.md`](../docs/handoff-adapters.md) and
[`../handoff-cli/README.md`](../handoff-cli/README.md). Not yet published
to PyPI.

See the [root README](https://github.com/EvAlssment/boxkite#readme) for
what boxkite is and the full self-hosting story.
