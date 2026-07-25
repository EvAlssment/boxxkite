# Stateful interpreters: `python_interpreter` and `node_interpreter`

Demonstrates boxkite's two **persistent, kept-alive** code-execution tools
side by side, with no agent framework and no LLM in the loop -- same style
as [`../raw_api`](../raw_api) and [`../hosted_control_plane`](../hosted_control_plane):
this calls each tool's framework-agnostic `ToolSpec.handler` directly
(`boxkite.tools.create_sandbox_tool_specs`) instead of routing through an
agent's reasoning loop, since the point here is the *statefulness* itself,
not tool-calling plumbing already covered by the other examples.

## What it does (`interpreters_demo.py`)

1. Creates a sandbox session via `SandboxManager`.
2. Builds the tool set via `create_sandbox_tool_specs(enable_node_interpreter=True)`.
3. Calls `python_interpreter` twice: the first call defines a Python list
   and sums it, the second call appends to *that same list* and sums it
   again -- proving the variable survived between calls.
4. Does the same thing against `node_interpreter` (opt-in, off by default)
   with a JS array, then shows that re-declaring the same `const` name in a
   third call raises a real JavaScript `SyntaxError` -- genuine top-level
   REPL scoping behavior, not a bug.

Contrast this with `bash_tool`'s `python3 -c ...`/`node -e ...`: those spawn
a fresh subprocess per call, so a variable from one call is *never* visible
in the next. `python_interpreter`/`node_interpreter` exist specifically for
tasks that need to build up state across multiple tool calls (e.g. parse a
large file once, then run several separate queries against it).

## Prerequisites

- `boxkite up` running (docker-compose sidecar reachable at `localhost:8080`),
  with the token it wrote to `~/.boxkite/local.env` -- see the main
  [README](../../README.md)'s quickstart.
- `pip install -e ../..` from the repo root (or `pip install -r
  requirements.txt`) -- no extras, no LLM API key needed.
- To see `node_interpreter` actually execute (rather than this script
  printing a "disabled" message and skipping that half of the demo): the
  **sidecar process itself** needs `BOXKITE_NODE_INTERPRETER_ENABLED=true`
  set in its environment before `boxkite up` / `docker compose up` starts
  it. This is new, off-by-default attack surface
  (`docs/NODE-INTERPRETER-DESIGN.md`) -- there is no way for this example
  script to turn it on for you from the outside, by design (the same
  two-layer opt-in `pty_exec` uses).

`python_interpreter` itself needs no extra sidecar configuration -- it's
always on, unlike `node_interpreter`.

## Run

```bash
export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
export RUNTIME_MODE=compose SIDECAR_URL=http://localhost:8080
python interpreters_demo.py
```

Expected output shape (Node half assumes `BOXKITE_NODE_INTERPRETER_ENABLED=true`
was set on the sidecar):

```
Creating sandbox session ... 
Tools wired: [..., 'python_interpreter', ..., 'node_interpreter']

== python_interpreter (always on) ==
call 1 (`orders = [10, 25, 40]; sum(orders)`) -> 75
call 2 (`orders.append(5); sum(orders)`)      -> 80
Confirmed: `orders` from call 1 was still there in call 2.

== node_interpreter (opt-in) ==
call 1 (`const orders = [...]; orders.reduce(...)`) -> 75
call 2 (`orders.push(5); orders.reduce(...)`)       -> 80
Confirmed: `orders` from call 1 was still there in call 2.

Re-declaring `const orders` in a THIRD call is a real JS error, ...
call 3 (`const orders = [];` again) -> Error:
...SyntaxError: Identifier 'orders' has already been declared...

Destroying session ...
```

## What's actually verified here

Being blunt about this, same as `examples/README.md`'s policy for the rest
of this cookbook: this script's tool wiring (`create_sandbox_tool_specs`,
both handlers' call signatures and return shapes) was exercised against
`tests/test_tools_adapters.py`'s fake sandbox managers and against a real
local `node` binary (see `tests/test_sidecar_node_interpreter.py` and
`tests/test_node_interpreter_tool.py`, which cover exactly the
persistence/redeclaration/error behavior this script demonstrates). This
script itself was **not** run against a live `boxkite up` docker-compose
stack with `BOXKITE_NODE_INTERPRETER_ENABLED=true` set in this environment
-- the same scope boundary already disclosed in
`docs/NODE-INTERPRETER-DESIGN.md`'s status header and
`docs/DAYTONA-COMPARISON.md` (tested against a real local `node` binary,
not yet exercised inside an actual Kubernetes pod or the
`deploy/sandbox.Dockerfile` image). If you run this against a real stack
and hit a mismatch, please file an issue with the exact output.
