"""Demonstrates boxkite's two persistent, kept-alive interpreter tools --
`python_interpreter` (always on) and `node_interpreter` (opt-in) -- side by
side, against a real sandbox pod.

No LangChain/LangGraph and no LLM in the loop, same style as `../raw_api`
and `../hosted_control_plane`: this calls each tool's framework-agnostic
`ToolSpec.handler` directly (see `boxkite.tools.create_sandbox_tool_specs`)
rather than routing through an agent's reasoning loop, because the point
here is the *statefulness* itself, not tool-calling plumbing already shown
by the other examples.

The core thing this proves: unlike `bash_tool`'s `python3 -c ...`/
`node -e ...` (a fresh subprocess every call -- nothing survives between
calls), a variable assigned in one `python_interpreter`/`node_interpreter`
call is still readable in the next call, because the sidecar keeps one
interpreter process alive per session until it's reset, idles out, or the
session itself is torn down. See docs/NODE-INTERPRETER-DESIGN.md and
python_interpreter_tool.py's own docstring for the full design.

Prerequisites:
  - `boxkite up` running (docker-compose sidecar reachable at localhost:8080),
    with the token it wrote to ~/.boxkite/local.env.
  - To see node_interpreter actually run (rather than the "disabled"
    message this script prints instead of failing): the sidecar process
    also needs BOXKITE_NODE_INTERPRETER_ENABLED=true set in its own
    environment before `boxkite up` -- this is new, off-by-default attack
    surface (see docs/NODE-INTERPRETER-DESIGN.md), not something this
    example script can turn on for you from the outside.
  - `pip install -r requirements.txt` (this repo's `boxkite` package only --
    no agent framework, no LLM API key needed).

Run:
    export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
    export RUNTIME_MODE=compose SIDECAR_URL=http://localhost:8080
    python interpreters_demo.py
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from boxkite import SandboxManager
from boxkite.tools import create_sandbox_tool_specs

# The sidecar 404s every /node-interpreter/* route unless this env var is
# set on it -- when that happens, node_interpreter_exec() raises here. This
# is the substring the sidecar's own 404 body contains (see sidecar/main.py),
# used below to tell "the feature is off" apart from any other failure.
_NODE_INTERPRETER_DISABLED_MARKER = "node-interpreter"


async def _demo_python_interpreter(specs: list) -> None:
    print("== python_interpreter (always on) ==")
    spec = next(s for s in specs if s.name == "python_interpreter")

    first = await spec.handler(code="orders = [10, 25, 40]\nsum(orders)")
    print(f"call 1 (`orders = [10, 25, 40]; sum(orders)`) -> {first}")

    second = await spec.handler(code="orders.append(5)\nsum(orders)")
    print(f"call 2 (`orders.append(5); sum(orders)`)      -> {second}")

    assert second.strip() == "80", (
        f"expected the second call to see `orders` from the first call "
        f"(total 80), got {second!r} -- state did not persist"
    )
    print("Confirmed: `orders` from call 1 was still there in call 2.\n")


async def _demo_node_interpreter(specs: list) -> None:
    print("== node_interpreter (opt-in) ==")
    spec = next((s for s in specs if s.name == "node_interpreter"), None)
    if spec is None:
        print(
            "node_interpreter wasn't wired into this tool set -- pass "
            "enable_node_interpreter=True to create_sandbox_tool_specs "
            "(this script already does; see the code below if you copied "
            "just this branch).\n"
        )
        return

    first = await spec.handler(code="const orders = [10, 25, 40];\norders.reduce((a, b) => a + b, 0)")
    if _NODE_INTERPRETER_DISABLED_MARKER in first and "Error" in first:
        print(
            f"node_interpreter call failed: {first}\n"
            "This almost always means BOXKITE_NODE_INTERPRETER_ENABLED=true "
            "isn't set on the sidecar process itself -- see this file's "
            "module docstring. Skipping the rest of the Node demo.\n"
        )
        return
    print(f"call 1 (`const orders = [...]; orders.reduce(...)`) -> {first}")

    second = await spec.handler(code="orders.push(5);\norders.reduce((a, b) => a + b, 0)")
    print(f"call 2 (`orders.push(5); orders.reduce(...)`)       -> {second}")

    assert second.strip() == "80", (
        f"expected the second call to see `orders` from the first call "
        f"(total 80), got {second!r} -- state did not persist"
    )
    print("Confirmed: `orders` from call 1 was still there in call 2.\n")

    print("Re-declaring `const orders` in a THIRD call is a real JS error, ")
    print("not a boxkite bug -- same as typing the same `const` twice into ")
    print("a real Node REPL:")
    redeclare = await spec.handler(code="const orders = [];")
    print(f"call 3 (`const orders = [];` again) -> {redeclare}\n")


async def main() -> None:
    manager = SandboxManager()
    session_id = str(uuid4())
    organization_id = uuid4()

    print(f"Creating sandbox session {session_id} ...")
    await manager.create_session(organization_id=organization_id, session_id=session_id)

    try:
        specs = create_sandbox_tool_specs(
            sandbox_manager=manager,
            organization_id=organization_id,
            session_id=session_id,
            # Off by default at two layers (this factory flag, AND the
            # sidecar's own BOXKITE_NODE_INTERPRETER_ENABLED env var) -- see
            # create_sandbox_tool_specs's enable_node_interpreter docstring.
            enable_node_interpreter=True,
        )
        print(f"Tools wired: {[s.name for s in specs]}\n")

        await _demo_python_interpreter(specs)
        await _demo_node_interpreter(specs)

    finally:
        print(f"Destroying session {session_id} ...")
        await manager.destroy_session(session_id)


if __name__ == "__main__":
    asyncio.run(main())
