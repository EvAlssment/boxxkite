"""LlamaIndex ReActAgent driving a boxkite sandbox.

Uses boxkite.tools.adapters.to_llamaindex_tools() to convert boxkite's
framework-agnostic ToolSpecs into LlamaIndex FunctionTool objects, then
hands them to llama_index.core.agent.workflow.ReActAgent -- the same
"wrap a sandbox as a FunctionTool, use it inside a ReAct agent" pattern
E2B's own LlamaIndex cookbook example follows (see docs/E2B-COMPARISON.md
§4.2), just against boxkite's tool surface instead of a single
`execute_python` tool.

Task: same as ../langchain_tool_calling and ../openai_function_calling --
write a short Python script to a file and run it, using only bash_tool
and file_create.

Prerequisites:
  - `boxkite up` running.
  - `pip install -e "../..[llamaindex]"` and `pip install -r requirements.txt`.
  - OPENAI_API_KEY set (or swap the Llm below for another LlamaIndex LLM
    integration -- see requirements.txt).

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

from llama_index.core.agent.workflow import ReActAgent
from llama_index.llms.openai import OpenAI

from boxkite import SandboxManager
from boxkite.tools.adapters import to_llamaindex_tools
from boxkite.tools.bash_tool import create_bash_tool_spec
from boxkite.tools.file_tools import create_file_create_tool_spec

TASK = (
    "Write a file at /workspace/greet.py containing a Python script that "
    "prints 'hello from boxkite' and then prints the current UTC date using "
    "the datetime module. Then run it. Tell me exactly what it printed."
)


async def main() -> None:
    model_name = os.environ.get("BOXKITE_EXAMPLE_MODEL", "gpt-5")
    llm = OpenAI(model=model_name)

    manager = SandboxManager()
    session_id = str(uuid4())

    print(f"Creating sandbox session {session_id} ...")
    await manager.create_session(organization_id=uuid4(), session_id=session_id)

    try:
        specs = [
            create_bash_tool_spec(session_id=session_id, sandbox_manager=manager),
            create_file_create_tool_spec(session_id=session_id, sandbox_manager=manager),
        ]
        tools = to_llamaindex_tools(specs)
        print(f"Tools wired: {[t.metadata.name for t in tools]}")

        agent = ReActAgent(tools=tools, llm=llm)

        print("Running agent...\n" + "-" * 60)
        result = await agent.run(user_msg=TASK)

        print(str(result))
    finally:
        print(f"Destroying session {session_id} ...")
        await manager.destroy_session(session_id)


if __name__ == "__main__":
    asyncio.run(main())
