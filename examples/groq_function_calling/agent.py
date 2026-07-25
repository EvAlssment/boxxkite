"""Native Groq function-calling loop against a boxkite sandbox.

No LangChain/LangGraph anywhere in this file -- boxkite.tools.adapters.
to_openai_functions() is pure stdlib (it only builds the
{"type": "function", "function": {...}} schema OpenAI's API expects), and
Groq's own SDK is intentionally OpenAI-compatible (a drop-in
`chat.completions.create(model=..., messages=..., tools=...)` shape --
verified against the installed `groq` 1.5.0 package: `Groq.chat.completions.
create` takes the same `messages`/`tools`/`tool_choice` parameters as
OpenAI's client, and `ChatCompletionMessage` is a pydantic model with the
same `tool_calls` / `model_dump()` shape), so this example is close to a
one-line swap of ../openai_function_calling's client for Groq's -- the
loop body is unchanged.

Task: same as ../openai_function_calling -- write a short Python script to
a file and run it, using only bash_tool and file_create.

Prerequisites:
  - `boxkite up` running.
  - `pip install -e ../..` (boxkite) and `pip install -r requirements.txt`.
  - GROQ_API_KEY set.

Run:
    export GROQ_API_KEY=...
    export RUNTIME_MODE=compose
    python agent.py

  (SandboxManager auto-loads the sidecar token + URL from
  ~/.boxkite/local.env, written by `boxkite up` — no manual export needed.)
"""

from __future__ import annotations

import asyncio
import json
import os
from uuid import uuid4

from groq import AsyncGroq

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
    model_name = os.environ.get("BOXKITE_EXAMPLE_MODEL", "llama-3.3-70b-versatile")
    client = AsyncGroq()

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
            response = await client.chat.completions.create(
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
