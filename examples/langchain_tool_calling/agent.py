"""Minimal LangChain agent using only 2 of boxkite's 15 default tools.

Deliberately smaller than ../langgraph_agent: no explicit graph, just
`langchain.agents.create_agent` (the current LangChain agent entry point,
same prebuilt ReAct loop LangGraph's create_react_agent uses under the
hood) wired to `bash_tool` and `file_create` only. This is the fastest path
to "see it work" -- one task, two tools, one file.

Task: ask the agent to write a short Python script to a file and run it.

Prerequisites:
  - `boxkite up` running.
  - `pip install -e ../..` (boxkite) and `pip install -r requirements.txt`.
  - ANTHROPIC_API_KEY set (or change init_chat_model's argument -- see
    requirements.txt).

Run:
    export ANTHROPIC_API_KEY=sk-ant-...
    export RUNTIME_MODE=compose
    python agent.py

  (SandboxManager auto-loads the sidecar token + URL from
  ~/.boxkite/local.env, written by `boxkite up` — no manual export needed.)
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

from langchain.agents import create_agent
from langchain.chat_models import init_chat_model

from boxkite import SandboxManager
from boxkite.tools.bash_tool import create_bash_tool
from boxkite.tools.file_tools import create_file_create_tool

TASK = (
    "Write a file at /workspace/greet.py containing a Python script that "
    "prints 'hello from boxkite' and then prints the current UTC date using "
    "the datetime module. Then run it. Tell me exactly what it printed."
)


async def main() -> None:
    model_name = os.environ.get("BOXKITE_EXAMPLE_MODEL", "anthropic:claude-sonnet-4-5")
    model = init_chat_model(model_name)

    manager = SandboxManager()
    session_id = str(uuid4())

    print(f"Creating sandbox session {session_id} ...")
    await manager.create_session(organization_id=uuid4(), session_id=session_id)

    try:
        # Only 2 of the 15 default tools -- narrower than the LangGraph
        # example on purpose, to show the tools are independently usable.
        tools = [
            create_bash_tool(session_id=session_id, sandbox_manager=manager),
            create_file_create_tool(session_id=session_id, sandbox_manager=manager),
        ]
        print(f"Tools wired: {[t.name for t in tools]}")

        agent = create_agent(model, tools)

        print("Running agent...\n" + "-" * 60)
        result = await agent.ainvoke({"messages": [{"role": "user", "content": TASK}]})

        print(result["messages"][-1].content)
    finally:
        print(f"Destroying session {session_id} ...")
        await manager.destroy_session(session_id)


if __name__ == "__main__":
    asyncio.run(main())
