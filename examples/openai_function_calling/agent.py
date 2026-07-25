"""Native OpenAI function-calling loop against a boxkite sandbox.

No LangChain/LangGraph anywhere in this file -- boxkite.tools.adapters.
to_openai_functions() is pure stdlib (it only builds the
{"type": "function", "function": {...}} schema OpenAI's API expects), so
this example wires the sandbox's tools straight into the OpenAI SDK's
`tools=` parameter and dispatches each `tool_call` back to the matching
ToolSpec.handler() by name. This is the same shape as E2B's own
"connect-llms" function-calling cookbook pattern, just against boxkite's
tool surface instead of a single execute_python tool.

Task: same as ../langchain_tool_calling -- write a short Python script to
a file and run it, using only bash_tool and file_create.

Prerequisites:
  - `boxkite up` running.
  - `pip install -e ../..` (boxkite) and `pip install -r requirements.txt`.
  - OPENAI_API_KEY set.

Run:
    export OPENAI_API_KEY=sk-...
    export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
    export RUNTIME_MODE=compose SIDECAR_URL=http://localhost:8080
    python agent.py
"""

from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

from openai import OpenAI

from boxkite import SandboxManager
from boxkite.tools.adapters import to_openai_functions
from boxkite.tools.bash_tool import create_bash_tool_spec
from boxkite.tools.file_tools import create_file_create_tool_spec

TASK = (
    "Write a file at /workspace/greet.py containing a Python script that "
    "prints 'hello from boxkite' and then prints the current UTC date using "
    "the datetime module. Then run it. Tell me exactly what it printed."
)

MAX_TURNS = 8


async def main() -> None:
    model_name = os.environ.get("BOXKITE_EXAMPLE_MODEL", "gpt-5")
    client = OpenAI()

    manager = SandboxManager()
    session_id = str(uuid4())

    print(f"Creating sandbox session {session_id} ...")
    await manager.create_session(organization_id=uuid4(), session_id=session_id)

    try:
        specs = [
            create_bash_tool_spec(session_id=session_id, sandbox_manager=manager),
            create_file_create_tool_spec(session_id=session_id, sandbox_manager=manager),
        ]
        specs_by_name = {spec.name: spec for spec in specs}
        tool_schema = to_openai_functions(specs)
        print(f"Tools wired: {list(specs_by_name)}")

        messages = [{"role": "user", "content": TASK}]

        print("Running agent...\n" + "-" * 60)
        for _ in range(MAX_TURNS):
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                tools=tool_schema,
            )
            choice = response.choices[0].message
            messages.append(choice.model_dump(exclude_none=True))

            if not choice.tool_calls:
                print(choice.content)
                return

            for tool_call in choice.tool_calls:
                spec = specs_by_name[tool_call.function.name]
                arguments = json.loads(tool_call.function.arguments)
                result = await spec.handler(**arguments)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call.id,
                        "content": str(result),
                    }
                )

        print("Reached MAX_TURNS without a final answer.")
    finally:
        print(f"Destroying session {session_id} ...")
        await manager.destroy_session(session_id)


if __name__ == "__main__":
    asyncio.run(main())
