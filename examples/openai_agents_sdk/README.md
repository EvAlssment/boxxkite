# OpenAI Agents SDK example

Closes the "OpenAI Agents SDK native provider" row in
`docs/E2B-COMPARISON.md` §5 — **at the function-tool level, not the deeper
sandbox-provider level**. Read that row before assuming this is full parity
with E2B's integration: E2B is one of several vendors registered as a
native `BaseSandboxSession` the SDK drives directly (exec + workspace
archive read/write + PTY streaming + mount policies, as one object).
boxkite's `to_openai_agents_tools()` wraps its existing `ToolSpec`s as
plain `agents.tool.FunctionTool` objects instead — a real, working
integration, but a shallower one, honestly scoped rather than oversold.
The deeper integration is blocked on two gaps this repo tracks separately
(agent-callable PTY, volume/mount support) — see the comparison doc.

## What it does

Same task as `../langchain_tool_calling`, `../openai_function_calling`, and
`../llamaindex_agent`: ask the agent to write a small Python script to a
file and run it, then report exactly what it printed. `to_openai_agents_tools()`
converts boxkite's two `ToolSpec`s (`bash_tool`, `file_create`) into
`FunctionTool` objects (`strict_json_schema=False` — see the adapter's own
docstring for why), handed to `agents.Agent(tools=[...])` and driven by
`agents.Runner.run(...)`.

## Prerequisites

1. A running boxkite stack: `boxkite up` from the repo root.
2. `pip install -e "../..[openai-agents]"` (boxkite itself, with the
   `openai-agents` extra) then `pip install -r requirements.txt`.
3. `OPENAI_API_KEY` set.

## Run

```bash
boxkite up
export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
export RUNTIME_MODE=compose
export SIDECAR_URL=http://localhost:8080
export OPENAI_API_KEY=sk-...

python agent.py
```

## What's verified vs. what needs a live run

Imports and tool wiring were verified against the actual installed
`boxkite`/`openai-agents` (0.18.2) package versions in this environment:
`to_openai_agents_tools()` was exercised against a fake sandbox manager
(both tools round-trip correctly through `on_invoke_tool`, including the
image-result-to-text fallback), and `Agent(tools=..., model=...)`
constructs without error. The end-to-end agent run against a live OpenAI
model was **not** exercised in this environment (no API key available);
please verify with your own key before relying on it.
