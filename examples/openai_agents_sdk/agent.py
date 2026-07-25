"""OpenAI Agents SDK, using boxkite tools as agents.tool.FunctionTools.

Closes the "OpenAI Agents SDK native provider" row in docs/E2B-COMPARISON.md
§5 at the *function-tool* level: `to_openai_agents_tools()` wraps boxkite's
ToolSpecs as `agents.tool.FunctionTool` objects, handed straight to
`agents.Agent(tools=[...])`. This is deliberately NOT the same thing as
E2B's deeper integration (a `BaseSandboxSession` the SDK drives directly,
covering exec + workspace archive read/write + PTY streaming + mount
policies as one object) -- see adapters.py's own module docstring and the
comparison doc for exactly why that's a separate, larger, currently-blocked
effort (it depends on the agent-callable-PTY and volume-mount gaps also
tracked in that doc), not something this example quietly pretends to be.

Task: same as ../langchain_tool_calling and ../openai_function_calling --
write a short Python script to a file and run it, using only bash_tool and
file_create.

Prerequisites:
  - `boxkite up` running.
  - `pip install -e "../..[openai-agents]"` and `pip install -r requirements.txt`.
  - OPENAI_API_KEY set.

Run:
    export OPENAI_API_KEY=sk-...
    export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
    export RUNTIME_MODE=compose SIDECAR_URL=http://localhost:8080
    python agent.py
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

from agents import Agent, Runner

from boxkite import SandboxManager
from boxkite.tools.adapters import to_openai_agents_tools
from boxkite.tools.bash_tool import create_bash_tool_spec
from boxkite.tools.file_tools import create_file_create_tool_spec

TASK = (
    "Write a file at /workspace/greet.py containing a Python script that "
    "prints 'hello from boxkite' and then prints the current UTC date using "
    "the datetime module. Then run it. Tell me exactly what it printed."
)


async def main() -> None:
    model_name = os.environ.get("BOXKITE_EXAMPLE_MODEL", "gpt-5")

    manager = SandboxManager()
    session_id = str(uuid4())

    print(f"Creating sandbox session {session_id} ...")
    await manager.create_session(organization_id=uuid4(), session_id=session_id)

    try:
        specs = [
            create_bash_tool_spec(session_id=session_id, sandbox_manager=manager),
            create_file_create_tool_spec(session_id=session_id, sandbox_manager=manager),
        ]
        tools = to_openai_agents_tools(specs)
        print(f"Tools wired: {[t.name for t in tools]}")

        agent = Agent(
            name="boxkite-sandbox-agent",
            instructions="You have access to a sandboxed bash shell and file writer.",
            tools=tools,
            model=model_name,
        )

        print("Running agent...\n" + "-" * 60)
        result = await Runner.run(agent, TASK)

        print(result.final_output)
    finally:
        print(f"Destroying session {session_id} ...")
        await manager.destroy_session(session_id)


if __name__ == "__main__":
    asyncio.run(main())
