# AGENTS.md — mcp-server

Guide for an AI coding agent working in `mcp-server/`. For DCO sign-off
and the fork/PR flow, see the repo root's
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Setup

Depends on `sdk-python` as a local sibling, same relationship
`control-plane/` has to the root package:

```bash
pip install -e ./sdk-python
cd mcp-server
pip install -e ".[dev]"
```

## Test

```bash
pytest tests/
```

`tests/live_smoke.py` is a separate, non-CI manual script that exercises a
real running control-plane, gated on real `BOXXKITE_BASE_URL` and
`BOXXKITE_API_KEY` environment variables — it is deliberately not part of
a normal `pytest tests/` pass (it'll either be skipped or fail loudly
without those set; don't try to make it pass in a normal dev loop, and
don't add assertions to it expecting CI to run it).

## Conventions

This package's use of the `mcp` SDK (`from mcp.server.fastmcp import
FastMCP`) is distinct from `control-plane/`'s own use of the same SDK for
its hosted `/mcp` Streamable HTTP endpoint (`control-plane/src/control_plane/hosted_mcp.py`)
— this package wraps a boxxkite sandbox as a local stdio MCP server, the
control-plane's is a separate, network-facing use of the same underlying
library. Don't conflate the two when changing either.
