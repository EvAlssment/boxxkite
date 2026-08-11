# Google ADK (Agent Development Kit) example

Demonstrates using `boxxkite.tools.adapters.to_google_adk_tools` to expose
`boxxkite`'s framework-agnostic sandbox tools (`bash_tool`, `file_create`,
etc.) to a Google ADK `Agent`, driven by the ADK `Runner`.

## What it does

Same task as `../llamaindex_agent` and `../openai_agents_sdk`: asks the agent
to write a small Python script to a file inside the isolated sandbox, run it
via `bash_tool`, and return the exact output.

`to_google_adk_tools()` converts boxxkite `ToolSpec` objects into ADK
`FunctionTool(func=...)` instances. ADK's `Runner` manages the multi-turn
model ↔ tool loop; the agent sees real execution results through boxxkite's
pod-isolated sidecar.

## Prerequisites

1. A running boxxkite stack: `boxxkite up` from the repo root (see main
   README's "Quickstart: docker-compose" section).
2. `pip install -e "../..[google-adk]"` (boxxkite with the `google-adk`
   extra) then `pip install -r requirements.txt`.
3. `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) set.

## Run

```bash
boxxkite up
export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxxkite/local.env | cut -d= -f2)
export RUNTIME_MODE=compose
export SIDECAR_URL=http://localhost:8080
export GEMINI_API_KEY=...

python agent.py
```

Expect output resembling:

```text
Creating sandbox session <uuid> ...
Tools wired: ['bash_tool', 'file_create']
Running agent...
------------------------------------------------------------
The script printed:
hello from boxxkite
2026-08-11
Destroying session <uuid> ...
```

## ADK Sandboxing vs. boxxkite

Google ADK handles multi-turn agent orchestration and function dispatch.
`boxxkite` provides the isolated execution environment for each tool call:

- **Pod-per-session container isolation** (Kubernetes / Docker Compose)
  preventing unconstrained host access.
- **Stateful interpreter sessions**, process management, and fine-grained
  network/storage policies.
- **Session-lifecycle semantics** preserved across agent turns.

If you are using raw `google-genai` *without* the ADK framework, use
`to_openai_functions()` instead and wire the dispatch loop yourself —
see `../gemini_function_calling/agent.py` for the canonical pattern.
