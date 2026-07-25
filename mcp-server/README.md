# boxkite-mcp

[![PyPI](https://img.shields.io/pypi/v/boxkite-mcp?label=PyPI)](https://pypi.org/project/boxkite-mcp/)

An MCP server over a hosted boxkite control-plane — lets any MCP-compatible
client (Claude Code, Claude Desktop, Codex, Cursor, etc.) attach a real
sandboxed code-execution backend as a native tool source, zero custom
integration code.

> **Prefer no local install?** A control-plane deployment built from this
> repo also exposes a **remote** Streamable HTTP MCP endpoint directly at
> `https://your-control-plane.example.com/mcp/` — add that URL to your MCP
> client's config instead of installing this package. See
> [`docs/HOSTED-MCP-DESIGN.md`](https://github.com/EvAlssment/boxkite/blob/main/docs/HOSTED-MCP-DESIGN.md).
> Use this package when you want the MCP server process running on your own
> machine instead.

## Install

```bash
pip install boxkite-mcp
# or, to run it as a standalone MCP server without a project venv:
pipx install boxkite-mcp
```

## Configuration

Two required environment variables:

| Variable | Meaning |
|---|---|
| `BOXKITE_BASE_URL` | Base URL of the boxkite control-plane |
| `BOXKITE_API_KEY` | A `bxk_live_...` API key for your account |

## Run

```bash
BOXKITE_BASE_URL=https://your-control-plane.example.com \
BOXKITE_API_KEY=bxk_live_... \
boxkite-mcp
```

Speaks MCP over stdio — point an MCP client's config at the `boxkite-mcp` command.

## Tools

Sandbox lifecycle and exec/file tools — `create_sandbox`, `destroy_sandbox`,
`get_sandbox`, `list_sandboxes`, `exec`, `file_create`, `view`, `str_replace`,
`ls`, `glob`, `grep` — every per-sandbox tool takes `session_id` as a
parameter, so the calling agent owns the full lifecycle within one
conversation.

Custom image tools (build a sandbox image with extra packages baked in,
then pass its id as `create_sandbox`'s `image_id`) — `create_sandbox_image`,
`get_sandbox_image`, `list_sandbox_images`, `delete_sandbox_image`.

Independent storage volume tools (create persistent storage mountable into
one or more sandboxes via `create_sandbox`'s `volume_mounts`) —
`create_sandbox_volume`, `get_sandbox_volume`, `list_sandbox_volumes`,
`delete_sandbox_volume`.

Outbound-MCP connection tools (grant a sandbox network egress to a curated
MCP catalog entry via `create_sandbox`'s `mcp_connection_names` — see
[`docs/OUTBOUND-MCP-DESIGN.md`](https://github.com/EvAlssment/boxkite/blob/main/docs/OUTBOUND-MCP-DESIGN.md);
there is no MCP-proxy transport yet, so this only widens network reachability,
it doesn't yet let the sandbox speak MCP protocol to the destination) —
`create_mcp_connection`, `list_mcp_connections`, `delete_mcp_connection`.

Language-server (LSP) tools for code intelligence inside a sandbox — start a
language server, open a file into it, request completions at a position, then
stop it — `lsp_start`, `lsp_open`, `lsp_completion`, `lsp_stop`. Like the other
per-sandbox tools, each takes `session_id`.

That's **26 tools** in total.

## Security

`exec` runs arbitrary shell commands with no client-side allowlist — the
isolation boundary is the sandbox itself (see the root repo's `SECURITY.md`),
not these MCP tools' argument validation. `exec`/`view` results are returned
to the calling LLM as plain, unsanitized text — treat sandbox output as
untrusted input, the same as a web-fetch or file-read tool's result.

## Related tools

Moving an in-progress local Claude Code/Codex CLI/opencode session (full
conversation history) into a fresh boxkite sandbox is **not** something
this MCP server can do as a tool call: a handoff adapter needs to read
local, on-disk CLI session state (e.g. Claude Code's
`~/.claude/projects/...` files) on the *user's own machine*, while an MCP
tool call runs wherever the MCP client invokes it, and `boxkite-mcp` itself
is a thin proxy to the hosted control-plane with no access to the calling
agent's local filesystem. That's handled instead by a separate, local-only
companion CLI, `boxkite-handoff` — see
[`../docs/handoff-adapters.md`](../docs/handoff-adapters.md) and
[`../handoff-cli/README.md`](../handoff-cli/README.md) for how it works.
Not yet published to PyPI.

## Development

```bash
pip install -e ".[dev]"
pytest tests/
```

See the [root README](https://github.com/EvAlssment/boxkite#readme) for
what boxkite is and the full self-hosting story.
