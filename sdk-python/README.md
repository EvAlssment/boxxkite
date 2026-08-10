# boxxkite-client

[![PyPI](https://img.shields.io/pypi/v/boxxkite-client?label=PyPI)](https://pypi.org/project/boxxkite-client/)

A Python client for a **hosted** boxxkite control-plane — create sandboxes,
run commands, edit files, over HTTP. Not the boxxkite package itself
(`boxxkite-sandbox`, which embeds `SandboxManager` against your own
Kubernetes cluster) — use this to talk to *someone else's* running
control-plane, hosted or self-hosted, over its API.

## Install

```bash
pip install boxxkite-client
pip install boxxkite-client[langchain]  # for create_sandbox_tools
```

## Quickstart

```python
from boxxkite_client import BoxxkiteClient

client = BoxxkiteClient(base_url="https://your-control-plane.example.com", api_key="bxk_live_...")

with client.sandbox(label="demo") as sb:
    result = sb.exec("python3 -c 'print(1 + 1)'")
    print(result["stdout"])  # "2\n"

    sb.file_create("notes.txt", "hello from boxxkite-client\n")
    print(sb.view("notes.txt")["content"])
# sandbox is destroyed automatically here, even if an exception was raised above
```

Also available: `AsyncBoxxkiteClient` (same shapes, `async`/`await`),
file/directory search (`ls`/`glob`/`grep`), long-running background
processes (`start_process`/`get_process_output`/`stop_process`), signed
preview URLs for exposing a port, an audit-log feed (`get_log`/`watch`),
interactive human takeover over a raw WebSocket, desktop (GUI) takeover
over the same raw-WebSocket pattern, secret management
(`create_secret`/`list_secrets`/`delete_secret`, for use via
`create_sandbox(secret_names=[...])`), filesystem snapshot management
(`create_snapshot`/`list_snapshots`/`get_snapshot`/`restore_snapshot`/
`delete_snapshot`), and a `create_sandbox_tools()`
LangChain factory. Full reference with examples
for all of these: [`docs/API.md`](https://github.com/EvAlssment/boxxkite/blob/main/docs/API.md).

## Error handling

Every non-2xx response raises `BoxxkiteApiError` (`.status_code`, `.code`,
`.message`). A network-level failure raises `BoxxkiteConnectionError`. Both
subclass `BoxxkiteError`.

```python
from boxxkite_client import BoxxkiteApiError

try:
    client.exec(sandbox["id"], "echo hi")
except BoxxkiteApiError as exc:
    if exc.code == "concurrent_sandbox_limit_reached":
        ...  # back off, destroy an old session, etc.
```

## Retries

Automatic retry is **off by default**. Pass a `RetryConfig` to enable it;
`RetryConfig()` carries sensible defaults (2 retries, exponential backoff
with full jitter, `Retry-After` honored):

```python
from boxxkite_client import BoxxkiteClient, RetryConfig

client = BoxxkiteClient(base_url="...", api_key="...", retry=RetryConfig())
```

Only idempotent verbs (`GET`/`HEAD`/`PUT`/`DELETE`/`OPTIONS`) are retried,
and only on a connection failure or a transient status (429, 500, 502, 503,
504) — a non-idempotent `POST` (create-sandbox/secret/webhook) is never
retried, so this can't double-create a resource. `AsyncBoxxkiteClient` takes
the same `retry=` argument and awaits its backoff. Every field of
`RetryConfig` is tunable if the defaults don't fit.

## Development

```bash
pip install -e ".[dev,langchain]"
pytest tests/
```

Tests mock the control-plane with `httpx.MockTransport` — no real deployment needed.

## Related tools

Moving an in-progress local Claude Code/Codex CLI/opencode/Cursor session
(full conversation history, not just a diff) into a fresh boxxkite sandbox
is handled by `boxxkite handoff <tool>`, part of the main `boxxkite` CLI
and built on this SDK — see
[`../docs/handoff-adapters.md`](../docs/handoff-adapters.md).

See the [root README](https://github.com/EvAlssment/boxxkite#readme) for
what boxxkite is and the full self-hosting story.

Questions, bug reports, or need a usage-limit bump? Join the
[Discord](https://discord.gg/JntfAx7cg5).
