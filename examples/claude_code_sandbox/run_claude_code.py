"""Run Claude Code headlessly inside a boxkite sandbox, against a real repo.

See ../../docs/CLAUDE-CODE-SANDBOX-QUICKSTART.md for the full write-up,
including why this needs the custom ../../deploy/sandbox-claude-code.Dockerfile
image (the declarative builder has no npm_packages field yet) and the
security caveat on how ANTHROPIC_API_KEY reaches the `claude` process (there
is no clean per-session secrets mechanism for arbitrary env vars yet --
this is a known, tracked gap, not an oversight).

Task: clone a small public repo, then ask Claude Code (headless, one-shot)
to summarize what the repo does, using only bash_tool (to invoke `claude`)
and git_clone (to fetch the repo with no raw credential ever touching a
shell string).

Prerequisites:
  - `docker build -f ../../deploy/sandbox-claude-code.Dockerfile -t
    boxkite-sandbox-claude-code ../..`
  - `boxkite up` with SANDBOX_IMAGE=boxkite-sandbox-claude-code (see the
    quickstart doc).
  - `pip install -e ../..` (boxkite itself; no extra needed -- this example
    uses only bash_tool and git_tools, no LangChain/LlamaIndex adapter).
  - ANTHROPIC_API_KEY set in *this* process's environment (not baked into
    the image) -- this script passes it into the sandboxed command itself.

Run:
    export ANTHROPIC_API_KEY=sk-ant-...
    export SIDECAR_AUTH_TOKEN=$(grep ^SIDECAR_AUTH_TOKEN= ~/.boxkite/local.env | cut -d= -f2)
    export RUNTIME_MODE=compose SIDECAR_URL=http://localhost:8080
    python run_claude_code.py
"""

from __future__ import annotations

import asyncio
import os
import shlex
from uuid import uuid4

from boxkite import SandboxManager
from boxkite.tools.bash_tool import create_bash_tool_spec
from boxkite.tools.git_tools import create_git_tool_specs

REPO_URL = "https://github.com/octocat/Hello-World.git"
PROMPT = "Summarize in two sentences what this repository contains."


async def main() -> None:
    api_key = os.environ["ANTHROPIC_API_KEY"]

    manager = SandboxManager()
    session_id = str(uuid4())

    print(f"Creating sandbox session {session_id} ...")
    await manager.create_session(organization_id=uuid4(), session_id=session_id)

    try:
        bash_spec = create_bash_tool_spec(session_id=session_id, sandbox_manager=manager)
        git_specs = {
            spec.name: spec
            for spec in create_git_tool_specs(session_id=session_id, sandbox_manager=manager)
        }

        print(f"Cloning {REPO_URL} ...")
        clone_result = await git_specs["git_clone"].handler(
            url=REPO_URL, path="/workspace/repo"
        )
        print(clone_result)

        # ANTHROPIC_API_KEY is prefixed on the command itself -- see the
        # module docstring and docs/CLAUDE-CODE-SANDBOX-QUICKSTART.md's
        # security note. This is the known, tracked gap, not a mistake.
        claude_command = (
            f"cd /workspace/repo && ANTHROPIC_API_KEY={shlex.quote(api_key)} "
            f"claude -p {shlex.quote(PROMPT)} --output-format json "
            f"--dangerously-skip-permissions"
        )
        print("Running Claude Code headlessly...\n" + "-" * 60)
        result = await bash_spec.handler(command=claude_command, timeout=180)
        print(result)
    finally:
        print(f"Destroying session {session_id} ...")
        await manager.destroy_session(session_id)


if __name__ == "__main__":
    asyncio.run(main())
