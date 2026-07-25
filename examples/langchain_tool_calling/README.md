# Minimal LangChain tool-calling example

The fastest path to seeing boxkite's tools work. One file, one task, two
tools (`bash_tool` and `file_create` -- not all 5), and `langchain.agents.
create_agent` instead of an explicit LangGraph graph. If you only have time
to run one example, and you want the simplest possible thing to read top to
bottom, start here; see `../langgraph_agent` for the full 5-tool version.

## What it does

Asks the agent to write a small Python script to a file and run it, then
report exactly what it printed. This exercises `file_create` (write the
script) and `bash_tool` (run it) -- the two tools most integrations reach
for first.

## Prerequisites

1. A running boxkite stack: `boxkite up` from the repo root (see the main
   README's "Quickstart: docker-compose" section).
2. `pip install -e ../..` (boxkite itself) then `pip install -r
   requirements.txt` (this example's LangChain deps).
3. `ANTHROPIC_API_KEY` set (or point `BOXKITE_EXAMPLE_MODEL` at another
   `init_chat_model`-supported provider and install its integration
   package).

## Run

```bash
boxkite up
# SandboxManager auto-loads the sidecar token + URL from ~/.boxkite/local.env
# (written by `boxkite up`), so no manual SIDECAR_AUTH_TOKEN export is needed.
export RUNTIME_MODE=compose
export ANTHROPIC_API_KEY=sk-ant-...

python agent.py
```

Expect output resembling:

```
Creating sandbox session <uuid> ...
Tools wired: ['bash_tool', 'file_create']
Running agent...
------------------------------------------------------------
The script printed:
hello from boxkite
2026-07-08
Destroying session <uuid> ...
```

The exact date line will reflect the day you run it.

## What's verified vs. what needs a live run

Imports and tool construction (`create_bash_tool`, `create_file_create_tool`,
`create_agent`, `init_chat_model`) were verified against the actual
installed package versions in this repo -- no import errors, no signature
mismatches. Separately, `create_bash_tool` and `create_file_create_tool`'s
underlying tools were exercised directly against a real local
docker-compose sandbox pod in this environment (write a file, run it,
confirm the output) and confirmed working. The end-to-end agent run
against a live LLM was **not** exercised in this environment (no API key
available); please verify with your own key before relying on it.
