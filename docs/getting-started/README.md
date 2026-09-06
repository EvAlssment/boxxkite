# Getting started

This is the shortest path from a fresh checkout to a working sandbox tool
call. It is intentionally linear: install the package, start the local
sandbox, run a command, then choose what to read next.

## 1. Install

Prerequisites:

- Python 3.11 or newer
- Docker, with the Docker daemon running

From a shell:

```bash
git clone https://github.com/EvAlssment/boxxkite.git boxxkite
cd boxxkite
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

The distribution is named `boxxkite-sandbox`; the import path and CLI command
are both `boxxkite`.

## 2. Start the first sandbox

Start the local Docker Compose stack:

```bash
boxxkite up
```

This starts the local sandbox, sidecar, and storage services used by the
Compose development path. Leave this command running if it stays attached in
your terminal; otherwise open a second shell, activate `.venv`, and continue.

## 3. Make the first tool call

Run a command inside the sandbox:

```bash
boxxkite exec "python3 -c 'print(1 + 1)'"
```

You can also inspect the sandbox filesystem:

```bash
boxxkite files ls /
```

If both commands return successfully, the CLI, sidecar, and sandbox are
connected. The same operations are available through the framework-agnostic
tool surface; [the raw API example](../../examples/basic/raw_api/) shows the HTTP
shape without adding an agent framework.

## 4. Choose the next path

- Add an agent framework: browse [the examples cookbook](../../examples/).
- Call the sidecar directly: use the [raw HTTP guide](../guides/raw-api.md).
- Run the real Kubernetes path locally: follow [the kind guide](../guides/local-kind.md).
- Put the API in front of multiple sessions: follow [the hosted control-plane guide](../guides/hosted-control-plane.md).
- Understand the security boundary first: read [the isolation model](../architecture/isolation-model.md) and [the security policy](../../SECURITY.md).

## If the quickstart stops working

The quickstart is the local Compose path. It does not create Kubernetes pods.
For a real cluster, use [Local kind](../guides/local-kind.md). For API and
authentication behavior without a cluster, use [the hosted control-plane
example](../../examples/basic/hosted_control_plane/), which explains its local
SQLite setup and its separate runtime requirements.
