"""Google ADK agent driving a boxxkite sandbox.

Uses boxxkite.tools.adapters.to_google_adk_tools() to convert boxxkite's
framework-agnostic ToolSpecs into Google ADK FunctionTool objects, then
registers them on an ADK Agent and runs it via the Runner.

Task: same as ../llamaindex_agent and ../gemini_function_calling --
write a short Python script to a file and run it, using only bash_tool
and file_create.

Prerequisites:
  - `boxxkite up` running.
  - `pip install -e "../..[google-adk]"` and `pip install -r requirements.txt`.
  - GEMINI_API_KEY (or GOOGLE_API_KEY) set.

Run:
    export GEMINI_API_KEY=...
    export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxxkite/local.env | cut -d= -f2)
    export RUNTIME_MODE=compose SIDECAR_URL=http://localhost:8080
    python agent.py
"""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService

from boxxkite import SandboxManager
from boxxkite.tools.adapters import to_google_adk_tools
from boxxkite.tools.bash_tool import create_bash_tool_spec
from boxxkite.tools.file_tools import create_file_create_tool_spec

TASK = (
    "Write a file at /workspace/greet.py containing a Python script that "
    "prints 'hello from boxxkite' and then prints the current UTC date using "
    "the datetime module. Then run it. Tell me exactly what it printed."
)


async def main() -> None:
    model_name = os.environ.get("BOXXKITE_EXAMPLE_MODEL", "gemini-2.5-flash")

    manager = SandboxManager()
    session_id = str(uuid4())

    print(f"Creating sandbox session {session_id} ...")
    await manager.create_session(organization_id=uuid4(), session_id=session_id)

    try:
        specs = [
            create_bash_tool_spec(session_id=session_id, sandbox_manager=manager),
            create_file_create_tool_spec(session_id=session_id, sandbox_manager=manager),
        ]
        tools = to_google_adk_tools(specs)
        print(f"Tools wired: {[t.name for t in tools]}")

        agent = Agent(
            name="boxxkite-sandbox-agent",
            model=model_name,
            instruction="You have access to a sandboxed bash shell and file writer.",
            tools=tools,
        )

        session_service = InMemorySessionService()
        adk_session = await session_service.create_session(
            app_name="boxxkite-example",
            user_id="local",
        )

        runner = Runner(
            agent=agent,
            app_name="boxxkite-example",
            session_service=session_service,
        )

        from google.genai.types import Content, Part

        print("Running agent...\n" + "-" * 60)
        async for event in runner.run_async(
            user_id="local",
            session_id=adk_session.id,
            new_message=Content(role="user", parts=[Part(text=TASK)]),
        ):
            if event.is_final_response() and event.content and event.content.parts:
                print(event.content.parts[0].text)
    finally:
        print(f"Destroying session {session_id} ...")
        await manager.destroy_session(session_id)


if __name__ == "__main__":
    asyncio.run(main())
