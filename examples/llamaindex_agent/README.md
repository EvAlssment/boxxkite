# LlamaIndex ReActAgent example

Closes a real, previously-undocumented gap noted in `docs/E2B-COMPARISON.md`
§4.2: E2B has an official LlamaIndex cookbook example (wrap the sandbox as
a `FunctionTool`, drive it from a `ReActAgent`); boxkite had no LlamaIndex
adapter at all until `boxkite.tools.adapters.to_llamaindex_tools` was added
alongside this example.

## What it does

Same task as `../langchain_tool_calling` and `../openai_function_calling`:
ask the agent to write a small Python script to a file and run it, then
report exactly what it printed. `to_llamaindex_tools()` converts boxkite's
two `ToolSpec`s (`bash_tool`, `file_create`) into LlamaIndex `FunctionTool`
objects (building a `pydantic` model from each tool's JSON-schema
parameters), which are then handed straight to
`llama_index.core.agent.workflow.ReActAgent`.

## Prerequisites

1. A running boxkite stack: `boxkite up` from the repo root (see the main
   README's "Quickstart: docker-compose" section).
2. `pip install -e "../..[llamaindex]"` (boxkite itself, with the
   `llamaindex` extra) then `pip install -r requirements.txt` (this
   example's `llama-index-llms-openai` dep).
3. `OPENAI_API_KEY` set (or swap in another LlamaIndex LLM integration --
   see `requirements.txt`).

## Run

```bash
boxkite up
export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
export RUNTIME_MODE=compose
export SIDECAR_URL=http://localhost:8080
export OPENAI_API_KEY=sk-...

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
2026-07-11
Destroying session <uuid> ...
```

## What's verified vs. what needs a live run

Imports and tool wiring were verified against the actual installed
`boxkite`/`llama-index-core`/`llama-index-llms-openai` package versions in
this environment: `to_llamaindex_tools()` was exercised against a fake
sandbox manager (both tools round-trip correctly, including the required-
vs-optional-parameter distinction in the generated pydantic schema), and
`ReActAgent(tools=..., llm=OpenAI(...))` constructs without error. The
end-to-end agent run against a live OpenAI model was **not** exercised in
this environment (no API key available); please verify with your own key
before relying on it.
