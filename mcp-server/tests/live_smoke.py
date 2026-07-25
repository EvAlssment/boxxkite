"""Manual, non-CI live smoke test -- drives the real MCP server process over
stdio via mcp's own ClientSession, against the real hosted boxkite
control-plane, using the test API key. Not collected by pytest (not
prefixed test_*, and it needs live credentials + network); run directly:

    BOXKITE_BASE_URL=... BOXKITE_API_KEY=... python tests/live_smoke.py

Exercises create_sandbox -> exec -> destroy_sandbox end to end.
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


async def main() -> int:
    base_url = os.environ["BOXKITE_BASE_URL"]
    api_key = os.environ["BOXKITE_API_KEY"]

    params = StdioServerParameters(
        command=sys.executable,
        args=["-m", "boxkite_mcp.server"],
        env={**os.environ, "BOXKITE_BASE_URL": base_url, "BOXKITE_API_KEY": api_key},
    )

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            tools = await session.list_tools()
            print("tools:", sorted(t.name for t in tools.tools))

            create_result = await session.call_tool("create_sandbox", {"label": "mcp-smoke-test"})
            create_text = create_result.content[0].text
            print("create_sandbox ->", create_text)
            if create_result.isError:
                print("FAIL: create_sandbox returned an error")
                return 1

            session_id = create_text.split()[2]
            print("session_id:", session_id)

            exec_result = await session.call_tool(
                "exec", {"session_id": session_id, "command": "python3 -c 'print(21 * 2)'"}
            )
            exec_text = exec_result.content[0].text
            print("exec ->", repr(exec_text))
            if exec_result.isError or "42" not in exec_text:
                print("FAIL: exec did not return the expected output")
                await session.call_tool("destroy_sandbox", {"session_id": session_id})
                return 1

            destroy_result = await session.call_tool("destroy_sandbox", {"session_id": session_id})
            destroy_text = destroy_result.content[0].text
            print("destroy_sandbox ->", destroy_text)
            if destroy_result.isError:
                print("FAIL: destroy_sandbox returned an error")
                return 1

            print("SMOKE TEST PASSED: create_sandbox -> exec -> destroy_sandbox all succeeded")
            return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
