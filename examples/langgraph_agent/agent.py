"""LangGraph agent wired to boxkite's 5 sandbox tools.

This is the headline example: `create_sandbox_tools(...)` returns plain
LangChain tools (bash_tool, file_create, view, str_replace, present_files),
handed straight to LangGraph's prebuilt ReAct agent (`create_react_agent`).
Nothing sandbox-specific happens on the LangGraph side — that's the point.

Task the agent is given: write a small CSV, write a data-processing script
that computes summary stats from it, run the script, and report the result.
That exercises all three of the tools an agent actually needs for real work
(file_create, bash_tool, view) end to end against a real sandbox pod.

Prerequisites:
  - `boxkite up` running (docker-compose sidecar reachable at localhost:8080),
    with the token it wrote to ~/.boxkite/local.env.
  - `pip install -r requirements.txt` (this repo's `boxkite` package, plus
    langgraph + a LangChain chat model integration).
  - An LLM API key. This example uses Anthropic's Claude by default
    (`ANTHROPIC_API_KEY`); swap `init_chat_model`'s argument for any other
    LangChain-supported provider (see requirements.txt comments).

Run:
    export ANTHROPIC_API_KEY=sk-ant-...
    export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
    export RUNTIME_MODE=compose SIDECAR_URL=http://localhost:8080
    python agent.py
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

from langchain.chat_models import init_chat_model
from langgraph.prebuilt import create_react_agent

from boxkite import SandboxManager
from boxkite.tools import create_sandbox_tools

TASK = """\
You have a sandbox with bash_tool, file_create, view, str_replace, and
present_files available.

1. Create /workspace/sales.csv with this content exactly:
   region,units,unit_price
   west,120,19.99
   east,85,24.50
   north,42,15.00
   south,201,9.99

2. Write /workspace/analyze.py: a script that reads sales.csv with the
   csv module (no pandas), computes total revenue per region
   (units * unit_price) and the overall total, and prints a small report
   to stdout, one line per region plus a TOTAL line, formatted like:
   west: $2398.80

3. Run it with bash_tool (`python3 /workspace/analyze.py`) and view the
   script you wrote with `view` to confirm it's what you intended.

4. Reply with the exact stdout from running the script as your final
   answer -- don't summarize or reformat it.
"""


async def main() -> None:
    model_name = os.environ.get("BOXKITE_EXAMPLE_MODEL", "anthropic:claude-sonnet-4-5")
    model = init_chat_model(model_name)

    manager = SandboxManager()
    session_id = str(uuid4())
    organization_id = uuid4()

    print(f"Creating sandbox session {session_id} ...")
    await manager.create_session(organization_id=organization_id, session_id=session_id)

    try:
        tools = create_sandbox_tools(
            sandbox_manager=manager,
            organization_id=organization_id,
            session_id=session_id,
        )
        print(f"Tools wired: {[t.name for t in tools]}")

        agent = create_react_agent(model, tools)

        print("Running agent...\n" + "-" * 60)
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": TASK}]},
            config={"recursion_limit": 25},
        )

        final_message = result["messages"][-1]
        print("-" * 60)
        print("Agent's final answer:\n")
        print(final_message.content)

        print("\nVerifying independently by viewing the file the agent wrote...")
        verification = await manager.execute(
            session_id=session_id,
            command="python3 /workspace/analyze.py",
            timeout=30,
        )
        print(f"Direct re-run exit_code={verification['exit_code']}")
        print(verification["stdout"])

    finally:
        print(f"Destroying session {session_id} ...")
        await manager.destroy_session(session_id)


if __name__ == "__main__":
    asyncio.run(main())
