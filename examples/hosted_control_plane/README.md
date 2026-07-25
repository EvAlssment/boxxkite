# Hosted control-plane: signup -> sandbox -> exec -> teardown

Shows the **hosted, multi-tenant** flow instead of the single-sidecar local
docker-compose flow (`../raw_api`): accounts, API keys, session ownership,
and per-account fair-use limits, all in front of the same `SandboxManager`
the other examples use directly.

As the main README says, boxkite has **no publicly running hosted
service** -- `control-plane/` is something you deploy yourself. This
example (and `boxkite signup`) work against whatever `CONTROL_PLANE_URL`
you point them at, including a local instance you spin up for this
walkthrough (below).

## What it does (`hosted_flow.py`)

1. `POST /v1/auth/signup` -- create an account, get a short-lived dashboard
   JWT.
2. `POST /v1/api-keys` (authenticated with that JWT, **not** an API key --
   you can't create a key using a key) -- get a long-lived API key.
3. `POST /v1/sandboxes` (authenticated with the API key from here on) --
   create a sandbox session. Response includes a `usage` block showing
   your fair-use consumption.
4. `POST /v1/sandboxes/{id}/exec` -- run a command.
5. `POST /v1/sandboxes/{id}/files` + `/files/view` -- write and read back a
   file.
6. `GET /v1/sandboxes` -- list your active sessions.
7. `DELETE /v1/sandboxes/{id}` -- tear the session down.

Every `/v1/sandboxes/*` call here is the exact HTTP-level equivalent of
`boxkite session create` / `boxkite exec` / `boxkite files *` / `boxkite
session rm` in hosted mode (see `src/boxkite/cli/cmd_session.py`,
`cmd_exec.py`, `cmd_files.py`) -- this script just shows the raw requests
without the CLI wrapper.

## How this differs from local docker-compose mode

| | Local (`../raw_api`, `boxkite up`) | Hosted (`control-plane/`) |
|---|---|---|
| Auth | One shared `SIDECAR_AUTH_TOKEN` | Per-account API keys, dashboard JWT for key management |
| Sessions | One implicit session (the compose sidecar itself) | Explicit `POST /v1/sandboxes` per session, multiple concurrent sessions per account |
| Ownership | N/A (single tenant) | Every route scoped to `account.id`; a foreign session_id 404s |
| Limits | None | `BOXKITE_MAX_CONCURRENT_SANDBOXES`, `BOXKITE_FREE_MONTHLY_SANDBOX_HOURS`, `BOXKITE_MAX_SESSION_MINUTES` reaper |
| Runtime | Docker Compose only | Whatever `SandboxManager`'s own `RUNTIME_MODE` is configured for (compose or real K8s) |

## Running a control-plane locally for this walkthrough

The control-plane defaults to SQLite for local dev (no separate Postgres
needed) and reuses `SandboxManager`'s own `RUNTIME_MODE`/`SIDECAR_URL`/
`SIDECAR_AUTH_TOKEN` env vars -- point it at the same local docker-compose
sidecar `boxkite up` already starts:

```bash
# 1. Start the sandbox runtime the control-plane will delegate to
boxkite up   # from the repo root

# 2. Set up the control-plane's own venv (from control-plane/)
cd control-plane
python3 -m venv .venv
.venv/bin/pip install -e .. -e '.[dev]'

# 3. Configure it -- replace the placeholder JWT_SECRET line in-place
#    (don't just `>>` a second JWT_SECRET= line after it; whichever line
#    wins depends on your shell's env-file parsing and it's easy to end up
#    debugging a "missing value" error that's actually a duplicate key)
cp .env.example .env
sed -i.bak "s/^JWT_SECRET=.*/JWT_SECRET=$(openssl rand -hex 32)/" .env && rm -f .env.bak

# 4. Point it at the sidecar boxkite up started
export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
export RUNTIME_MODE=compose
export SIDECAR_URL=http://localhost:8080
set -a; source .env; set +a

# 5. Run it (--app-dir src because control-plane's package lives under src/)
.venv/bin/uvicorn control_plane.main:app --host 0.0.0.0 --port 8090 --app-dir src
```

Then, from this directory:

```bash
pip install httpx
export CONTROL_PLANE_URL=http://localhost:8090
python hosted_flow.py
```

For a real deployment, replace step 2-5 with your actual `control-plane/`
deployment (see `control-plane/Dockerfile` and its own `.env.example`) and
just point `CONTROL_PLANE_URL` at it.

## What's verified

This entire flow was run end-to-end for real in this environment: a local
docker-compose sidecar (`boxkite up`'s underlying compose stack), a local
control-plane instance (SQLite backend, steps above), and `hosted_flow.py`
against it. Every step succeeded exactly as documented -- signup, API key
creation, sandbox creation (with a real `usage` block back), exec, file
create/view, list, and teardown. No LLM is involved in this example, so
there's no API-key-for-a-model dependency to worry about.
