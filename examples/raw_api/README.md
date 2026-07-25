# Raw sidecar HTTP API (no LangChain, no boxkite Python package)

For integrating with a different agent framework, or writing your own
tool-calling layer from scratch. These scripts talk directly to the
sidecar's HTTP API (`sidecar/main.py`) with no Python `boxkite` package and
no LangChain involved at all.

This is the local docker-compose sidecar (single sandbox, one shared
secret). For the multi-tenant, session-scoped hosted API, see
`../hosted_control_plane`.

## Files

- `curl_examples.sh` -- shell/curl walkthrough of `/health`, `/exec`,
  `/file-create`, `/view`, `/str-replace`.
- `requests_example.py` -- the same walkthrough in Python with the
  `requests` library, plus assertions on the responses so it doubles as a
  smoke test.

## The routes (as of `sidecar/main.py`)

Every route except `/health` requires the `X-Sidecar-Auth-Token` header
with the value `boxkite up` generated (or the one you put in `.env`
manually).

| Route | Request body | Response body |
|---|---|---|
| `GET /health` | -- | `{status, session_id, skills_rev, runtime_mode, storage_backend, idle_seconds}` |
| `POST /exec` | `{command: str, timeout: int = 30, description?: str}` | `{exit_code: int, stdout: str, stderr: str}` |
| `POST /file-create` | `{path: str, content: str, description?: str}` | `{path: str, size: int, created: bool}` |
| `POST /view` | `{path: str, view_range?: [start, end], description?: str}` | `{content: str, lines: int, is_directory: bool, entries?: [str]}` |
| `POST /str-replace` | `{path: str, old_str: str, new_str: str, replace_all?: bool, description?: str}` | `{path: str, replaced: bool, occurrences: int}` |
| `POST /present-files` | `{filepaths: [str]}` | `{files: [dict], copy_operations: [str]}` |

`/view` returning `is_directory: true` means `path` was a directory; its
`entries` field lists directory contents rather than `content`.

## Prerequisites

```bash
boxkite up   # from the repo root
```

## Run

```bash
./curl_examples.sh
# or
pip install requests
python requests_example.py
```

## What's verified

Both scripts were run against a real local docker-compose sidecar started
with `boxkite up`'s underlying `docker compose -f deploy/docker-compose.yml
up -d --build` in this environment. No LLM is involved in this example, so
there's no API-key dependency -- this is the one example in this
directory that was actually exercised end-to-end here, not just
statically checked.
