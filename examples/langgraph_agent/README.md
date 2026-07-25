# LangGraph agent with boxkite's 5 sandbox tools

The headline example: `boxkite.tools.create_sandbox_tools(...)` returns
plain LangChain `@tool`-decorated functions, handed directly to LangGraph's
prebuilt `create_react_agent`. No adapter layer, no boxkite-specific graph
nodes -- this is exactly the "hand these tools to any LangChain/LangGraph
agent" pitch from the main README, made concrete.

## What it does

The agent is given one task, in `agent.py`'s `TASK` prompt: write a small
CSV file, write a Python script that computes per-region revenue from it
(no pandas -- stdlib `csv` only, since the point is showing bash+file tools
working together, not the sandbox's data-science preinstalls), run the
script, and report back the exact output. This exercises `file_create`,
`bash_tool`, and `view` end to end against a real sandbox pod -- not a
mocked tool call.

After the agent finishes, the script independently re-runs the script via
`manager.execute(...)` directly (bypassing the agent) and prints that
output too, so you can see the agent's file actually persisted and actually
works -- not just that the LLM claimed it did.

## Prerequisites

1. **A running boxkite stack.** Either:
   - Local docker-compose: `boxkite up` (or `docker compose -f
     ../../deploy/docker-compose.yml up -d --build`), from the repo root.
   - Or a real Kubernetes cluster (see the main README's "Quickstart: real
     Kubernetes, via kind" section) -- set `RUNTIME_MODE=k8s` instead of
     `compose` and the corresponding `SANDBOX_IMAGE`/`SIDECAR_IMAGE`/proxy
     env vars.

2. **boxkite itself installed**, from the repo root:
   ```bash
   pip install -e ../..
   ```

3. **This example's extra dependencies**:
   ```bash
   pip install -r requirements.txt
   ```

4. **An LLM API key.** Defaults to Anthropic's Claude
   (`ANTHROPIC_API_KEY`). To use a different provider, install its
   LangChain integration package (e.g. `langchain-openai`) and set
   `BOXKITE_EXAMPLE_MODEL` to a provider-prefixed model string that
   [`init_chat_model`](https://docs.langchain.com/oss/python/langchain/models)
   understands, e.g. `openai:gpt-4o`.

## Run

```bash
# 1. Start boxkite (from the repo root)
boxkite up

# 2. Point this example at it -- boxkite up writes the token to ~/.boxkite/local.env
export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
export RUNTIME_MODE=compose
export SIDECAR_URL=http://localhost:8080

# 3. Set your LLM key
export ANTHROPIC_API_KEY=sk-ant-...

# 4. Run it
python agent.py
```

Expected output looks like:

```
Creating sandbox session <uuid> ...
Tools wired: ['bash_tool', 'file_create', 'view', 'str_replace', 'present_files']
Running agent...
------------------------------------------------------------
Agent's final answer:

west: $2398.80
east: $2082.50
north: $630.00
south: $2007.99
TOTAL: $7119.29
------------------------------------------------------------
Verifying independently by viewing the file the agent wrote...
Direct re-run exit_code=0
west: $2398.80
...
Destroying session <uuid> ...
```

The exact numbers are deterministic (fixed CSV in the prompt); the LLM's
prose framing around them is not, since it's still a live model call.

## Notes on the tool wiring

- `SandboxManager()` auto-detects compose vs. Kubernetes mode from
  `RUNTIME_MODE`/`SIDECAR_URL` env vars -- see `src/boxkite/manager.py`.
- `create_session(organization_id, session_id, ...)` claims a warm pod (K8s
  mode) or configures the single compose sidecar, and returns
  `{"pod_name": ...}`.
- `create_sandbox_tools(sandbox_manager=manager, organization_id=..., session_id=...)`
  is the one factory call that produces all 8 tools
  (`src/boxkite/tools/factory.py`); `audit_sink` and `work_item_id` are
  optional and omitted here since this example has no external system of
  record to mirror writes into.
- Always call `manager.destroy_session(session_id)` when done (see the
  `finally` block in `agent.py`) -- otherwise the pod either lingers in K8s
  mode or the compose sidecar's session state never resets.

## What's verified vs. what needs a live run

This example's imports and tool wiring were verified against the actual
installed `boxkite`/`langgraph`/`langchain`/`langchain-anthropic` versions
in this repo's `.venv` (no import errors, no signature mismatches).
Separately, the original 5 tools this example wires up (`bash_tool`,
`file_create`, `view`, `str_replace`, `present_files`) were exercised
directly (bypassing the LLM) against a real local docker-compose sandbox
pod in this environment and confirmed working -- file writes persisted,
`bash_tool` executed real Python inside the pod, `view` read the result
back correctly. The 3 newer tools (`ls`, `glob`, `grep`, added after that
verification pass) are covered by unit tests with a mocked
`SandboxManager` only -- this example doesn't call them, so they haven't
been exercised against a live pod as part of this cookbook.

What was **not** run end-to-end here is the LLM reasoning loop itself
(`create_react_agent` actually driving tool calls via Claude) -- there was
no `ANTHROPIC_API_KEY`/other provider key available in this environment.
Given the tool layer is independently confirmed working, running this
example with a real key is expected to work; if it doesn't, please open an
issue with the exact traceback.
