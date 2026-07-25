"""Run the Foundry/Anvil audit-sandbox example (GitHub issue #137) end to
end against a real boxkite sandbox: seed the pre-baked contracts project
into /workspace, run `forge build`/`forge test` (the "detect + verify the
patch" step), then run scripts/deploy_and_exploit.sh against a live,
deterministic local Anvil chain (the "exploit a persistent chain" step).

See README.md for the full write-up -- especially "Why one bash_tool call,
not start_process + many calls" and "What no live RPC egress actually
means here" before pointing this at anything but a local dev stack.

This script deliberately does NOT use an LLM or any agent framework --
same shape as ../claude_code_sandbox/run_claude_code.py: a deterministic,
scripted walkthrough of the same bash_tool calls a real audit agent would
make, so it's runnable with no API key at all. Swap the scripted commands
below for an LLM tool-calling loop (see ../langgraph_agent/agent.py for
that pattern) to turn this into an actual agent.

Prerequisites (see README.md's "Running it" section for the full detail,
including why compose mode needs a docker-compose.override.yml rather than
a SANDBOX_IMAGE env var):
  1. From the repo root:
       export SIDECAR_AUTH_TOKEN=$(openssl rand -hex 32)
       docker compose \\
         -f deploy/docker-compose.yml \\
         -f examples/foundry_audit_sandbox/docker-compose.override.yml \\
         up -d --build
  2. `pip install -e ../..` (boxkite itself; no extra needed).

Run (reuse the SAME SIDECAR_AUTH_TOKEN value from step 1 above):
    export SIDECAR_AUTH_TOKEN=<the same value exported in step 1>
    export RUNTIME_MODE=compose SIDECAR_URL=http://localhost:8080
    python run_audit_sandbox.py
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from boxkite import SandboxManager
from boxkite.tools.bash_tool import create_bash_tool_spec

SEED_COMMAND = "cp -r /opt/foundry-audit-template/. /workspace/"

BUILD_AND_TEST_COMMAND = (
    "cd /workspace/contracts && forge build && forge test -vv"
)

# All in ONE bash_tool call, deliberately -- see README.md's "Why one
# bash_tool call, not start_process + many calls" section. Anvil is
# started, deployed against, exploited, and killed within a single shell
# invocation, so it never depends on cross-/exec-call network reachability
# (which, in RUNTIME_MODE=k8s with the default
# SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED=true, a background process started
# via start_process would not have -- see process_tools.py's own
# documented non-goal).
EXPLOIT_COMMAND = "cd /workspace/contracts && bash /workspace/scripts/deploy_and_exploit.sh"


async def main() -> None:
    manager = SandboxManager()
    session_id = str(uuid4())
    organization_id = uuid4()

    print(f"Creating sandbox session {session_id} ...")
    await manager.create_session(organization_id=organization_id, session_id=session_id)

    try:
        bash_spec = create_bash_tool_spec(session_id=session_id, sandbox_manager=manager)

        print("\n== Seeding /workspace from the pre-baked template ==")
        print(await bash_spec.handler(command=SEED_COMMAND, timeout=30))

        print("\n== forge build + forge test (detect the bug, verify the patch) ==")
        print(await bash_spec.handler(command=BUILD_AND_TEST_COMMAND, timeout=60))

        print("\n== Deploying to a live, deterministic Anvil chain and exploiting it ==")
        print(await bash_spec.handler(command=EXPLOIT_COMMAND, timeout=60))

    finally:
        print(f"\nDestroying session {session_id} ...")
        await manager.destroy_session(session_id)


if __name__ == "__main__":
    asyncio.run(main())
