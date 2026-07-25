"""Native Gemini (google-genai) function-calling loop against a boxkite sandbox.

No LangChain/LangGraph anywhere in this file. Gemini's function-calling shape
is meaningfully different from OpenAI's: instead of a flat `messages` list
with `tool_calls` hanging off an assistant message, google-genai works in
`Content`/`Part` terms -- a turn is a `Content(role=..., parts=[Part(...)])`,
and a model's function call comes back as a `Part` whose `function_call`
field is a `FunctionCall(name=..., args=...)`. The reply goes back as another
`Content` (role "user") holding a `Part` built from
`Part.from_function_response(name=..., response={...})`, not a `{"role":
"tool", ...}` message.

This example still starts from boxkite's `to_openai_functions()` (the
framework-agnostic ToolSpec -> `{"type": "function", "function": {...}}`
schema, pure stdlib) and then unwraps each entry into a Gemini
`types.FunctionDeclaration(name=..., description=..., parameters_json_schema=...)`
-- `parameters_json_schema` accepts a raw JSON-schema dict directly (verified
against the installed `google-genai` 2.11.0: `FunctionDeclaration.parameters`
wants a `google.genai.types.Schema`, but `parameters_json_schema` takes
`Any` and passes a plain JSON schema through), so no schema-object
translation is needed beyond that unwrap.

Task: same as ../openai_function_calling -- write a short Python script to
a file and run it, using only bash_tool and file_create.

Prerequisites:
  - `boxkite up` running.
  - `pip install -e ../..` (boxkite) and `pip install -r requirements.txt`.
  - GEMINI_API_KEY (or GOOGLE_API_KEY) set.

Run:
    export GEMINI_API_KEY=...
    export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
    export RUNTIME_MODE=compose SIDECAR_URL=http://localhost:8080
    python agent.py
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

from google import genai
from google.genai import types

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


def _function_declarations(specs) -> list[types.FunctionDeclaration]:
    """Unwrap to_openai_functions() output into Gemini FunctionDeclarations.

    Reuses the same framework-agnostic schema construction as the OpenAI
    example, then adapts the flat OpenAI `{"type": "function", "function":
    {name, description, parameters}}` entries to Gemini's own
    `FunctionDeclaration` object shape.
    """
    declarations = []
    for entry in to_openai_functions(specs):
        fn = entry["function"]
        declarations.append(
            types.FunctionDeclaration(
                name=fn["name"],
                description=fn["description"],
                parameters_json_schema=fn["parameters"],
            )
        )
    return declarations


async def main() -> None:
    model_name = os.environ.get("BOXKITE_EXAMPLE_MODEL", "gemini-2.5-flash")
    client = genai.Client()

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
        config = types.GenerateContentConfig(
            tools=[types.Tool(function_declarations=_function_declarations(specs))]
        )
        print(f"Tools wired: {list(specs_by_name)}")

        contents: list[types.Content] = [
            types.Content(role="user", parts=[types.Part(text=TASK)])
        ]

        print("Running agent...\n" + "-" * 60)
        for _ in range(MAX_TURNS):
            response = await client.aio.models.generate_content(
                model=model_name,
                contents=contents,
                config=config,
            )
            candidate = response.candidates[0]
            contents.append(candidate.content)

            function_calls = response.function_calls
            if not function_calls:
                print(response.text)
                return

            response_parts = []
            for call in function_calls:
                spec = specs_by_name[call.name]
                result = await spec.handler(**(call.args or {}))
                response_parts.append(
                    types.Part.from_function_response(
                        name=call.name,
                        response={"result": str(result)},
                    )
                )
            contents.append(types.Content(role="user", parts=response_parts))

        print("Reached MAX_TURNS without a final answer.")
    finally:
        print(f"Destroying session {session_id} ...")
        await manager.destroy_session(session_id)


if __name__ == "__main__":
    asyncio.run(main())
