"""
Sandbox Tools Factory

Creates the complete set of sandbox tools for agent execution.
This is the main entry point for integrating sandbox tools into agents.

All execution happens via SandboxManager HTTP calls to the sidecar container.
Files are stored in the sidecar's own S3/Azure storage at:
  work-items/{org_id}/{work_item_id}/workspace/{path}

`create_sandbox_tool_specs()` is the framework-agnostic entry point: it
returns a list of `ToolSpec` (see ./types.py) with no LangChain dependency
at all. `create_sandbox_tools()` is kept for backward compatibility with
existing callers — it builds the same specs and adapts them into LangChain
tools via boxkite.tools.adapters.to_langchain_tools (requires the
`langchain` extra). Use boxkite.tools.adapters.to_openai_functions(specs)
for an OpenAI-style function-calling schema instead.
"""

import logging
from typing import Any, Optional, TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

from ..audit import AuditSink
from .bash_tool import create_bash_tool_spec
from .browser_tools import create_browser_tool_specs
from .file_tools import (
    create_file_create_tool_spec,
    create_view_tool_spec,
    create_str_replace_tool_spec,
)
from .git_tools import create_git_tool_specs
from .http_request_tool import create_http_request_tool_spec
from .lsp_tools import create_lsp_tool_specs
from .node_interpreter_tool import create_node_interpreter_tool_spec
from .present_files import create_present_files_tool_spec
from .pty_tools import create_pty_exec_tool_spec
from .process_tools import (
    create_get_process_output_tool_spec,
    create_list_processes_tool_spec,
    create_send_process_input_tool_spec,
    create_start_process_tool_spec,
    create_stop_process_tool_spec,
)
from .python_interpreter_tool import create_python_interpreter_tool_spec
from .run_tests_tool import create_run_tests_tool_spec
from .search_tools import (
    create_ls_tool_spec,
    create_glob_tool_spec,
    create_grep_tool_spec,
    create_watch_directory_tool_spec,
)
from .types import ToolSpec

logger = logging.getLogger(__name__)


def create_sandbox_tool_specs(
    sandbox_manager: Optional['SandboxManager'] = None,
    audit_sink: Optional[AuditSink] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    session_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    llm_client: Optional[Any] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
    allowed_commands: Optional[list] = None,
    enable_git_tools: bool = False,
    enable_http_request_tool: bool = False,
    enable_secret_env: bool = False,
    enable_agent_pty: bool = False,
    enable_node_interpreter: bool = False,
    enable_run_tests: bool = False,
    enable_browser_tool: bool = False,
    enable_lsp_tools: bool = False,
) -> list[ToolSpec]:
    """
    Create the complete, framework-agnostic set of sandbox ToolSpecs.

    This is the main factory function that builds:
    - bash_tool: Execute commands (python, node, shell)
    - python_interpreter: Execute code against a persistent, kept-alive
      Python interpreter (variables survive across calls, unlike bash_tool)
    - file_create: Create/overwrite files
    - view: View file contents
    - str_replace: Edit files
    - present_files: Generate download URLs
      (and image references for multimodal analysis)
    - ls: List direct children of a directory
    - glob: Find files by name pattern
    - grep: Search file contents by regex
    - (opt-in, see enable_git_tools) git_clone/git_status/git_add/
      git_commit/git_push/git_pull/git_branch/git_checkout
    - start_process: Start a tracked background process (dev server, watcher, REPL)
    - get_process_output: Poll a background process's output
    - send_process_input: Write to a background process's stdin
    - stop_process: Stop a background process
    - list_processes: List tracked background processes
    - watch_directory: Long-poll for a filesystem change (docs/FILE-WATCHER-DESIGN.md)
    - (opt-in, see enable_run_tests) run_tests: Run a test command and parse
      its output into a structured schema instead of raw stdout (see
      run_tests_tool.py; only pytest output is parsed so far)

    Each ToolSpec's `handler` is a plain async callable — no LangChain,
    LangGraph, CrewAI, or AutoGen import anywhere in this call path. Call
    `handler(**kwargs)` directly, pass `spec.parameters` to any
    OpenAI-style function-calling API, or convert the whole list with an
    adapter from boxkite.tools.adapters (`to_langchain_tools`,
    `to_openai_functions`).

    All execution routes through SandboxManager HTTP calls to the sidecar.
    Files are stored in the sidecar's own S3/Azure storage at:
        work-items/{org_id}/{work_item_id}/workspace/{path}

    Args:
        sandbox_manager: SandboxManager instance (required unless lazy_runtime is provided)
        audit_sink: Optional AuditSink (see ../audit.py) to mirror file writes
            into an external system — a database, a UI file browser, an audit
            log, etc. Entirely optional; every tool works with storage alone.
        organization_id: Organization ID (for storage path scoping)
        work_item_id: Work item ID (for file persistence)
        session_id: Optional session ID string for audit trail (may include prefix like "execution:")
        agent_name: Optional agent name for audit trail
        llm_client: Deprecated (kept for backward-compatible function signature)
        lazy_runtime: Optional LazySandboxRuntime shared across agent/subagents
        allowed_commands: Optional per-agent command allowlist for bash_tool.
            When non-empty, bash_tool runs in whitelist mode (only the listed
            program names may appear in command positions).
        enable_git_tools: Opt in to the git tool set (see
            src/boxkite/tools/git_tools.py). Off by default -- clone/push/
            pull additionally require the operator to have configured
            sandbox network egress to the target git host (see
            deploy/network-policy.yaml), which is also off by default.
        enable_http_request_tool: Opt in to the secrets-broker http_request
            tool (docs/SECRETS-DESIGN.md, src/boxkite/tools/http_request_tool.py).
            Off by default -- this is a new outbound-request surface from
            the sidecar (gated by the session's own secret_names grants and
            the sidecar's DNS-rebinding-safe destination check), distinct
            from bash_tool's network-isolated exec path, so it's opt-in the
            same way enable_git_tools is.
        enable_secret_env: Opt in to bash_tool's `secret_env` parameter
            (docs/SECRETS-DESIGN.md's bash_tool addendum) -- lets the agent
            reference a granted secret BY NAME to have it injected into a
            command's own process environment, resolved server-side by the
            sidecar, never a literal value passing through this process or
            appearing in the command string. Off by default, same
            new-credential-consumption-surface convention as
            enable_http_request_tool above.
        enable_agent_pty: Opt in to the `pty_exec` tool
            (docs/AGENT-PTY-DESIGN.md) -- runs one command behind a real
            pseudo-terminal, for programs that check `isatty()` and don't
            behave correctly over bash_tool's plain pipe. Off by default,
            AND separately requires `BOXKITE_AGENT_PTY_ENABLED=true` on the
            sidecar itself -- this flag alone does not turn the route on.
        enable_node_interpreter: Opt in to the `node_interpreter` tool
            (docs/NODE-INTERPRETER-DESIGN.md) -- the Node.js counterpart to
            the always-on `python_interpreter` tool: a persistent,
            kept-alive Node.js process per session (variables/top-level
            declarations survive across calls, unlike bash_tool's
            `node -e ...`). Off by default, AND separately requires
            `BOXKITE_NODE_INTERPRETER_ENABLED=true` on the sidecar itself --
            this flag alone does not turn the route on, same two-layer
            gating convention as enable_agent_pty above. Unlike
            python_interpreter, this is opt-in at the factory layer too:
            it's new attack surface (a second kept-alive-interpreter code
            path) introduced after this project's "flag new surface off by
            default" convention was established, so it doesn't inherit
            python_interpreter's always-on precedent.
        enable_run_tests: Opt in to the `run_tests` tool
            (src/boxkite/tools/run_tests_tool.py, docs/issue #123) -- runs a
            test command through the exact same exec primitive and command
            whitelist/blocked-command checks bash_tool uses, then parses the
            output into a structured {passed, failed, errors, failures,
            duration_seconds} schema instead of raw stdout. Off by default
            per this project's "new tool surface is an explicit opt-in"
            convention, even though it introduces no new exec path of its
            own (it's a parsing layer over bash_tool's own primitive).
        enable_browser_tool: Opt in to the browser_navigate/browser_exec/
            browser_screenshot/browser_close tool set
            (docs/BROWSER-EXEC-DESIGN.md, src/boxkite/tools/browser_tools.py)
            -- a headless Chromium process driven via Playwright/CDP. Off
            by default, AND separately requires
            `BOXKITE_BROWSER_ENABLED=true` on the sidecar itself -- this
            flag alone does not turn the routes on, same two-layer gating
            convention as enable_agent_pty/enable_node_interpreter above.
            This is the riskiest opt-in tool this repo ships (see the
            design doc's §3/§5): unlike every other egress-needing tool
            here, the browser needs broad, non-enumerable HTTPS/DNS
            egress with no per-request application-layer backstop -- an
            operator enabling this should also have provisioned the
            NetworkPolicy src/boxkite/browser_network_policy.py builds for
            browser-enabled sessions, not rely on this flag alone.
        enable_lsp_tools: Opt in to the `lsp_start`/`lsp_completion`/
            `lsp_stop` tool set (docs/LSP-SUPPORT-SCOPING.md,
            src/boxkite/tools/lsp_tools.py, GitHub issue #183) -- agent-
            invokable code completions against a persistent language
            server (pyright for Python, typescript-language-server for
            TypeScript/JS). Off by default, AND separately requires
            `BOXKITE_LSP_ENABLED=true` on the sidecar itself -- this
            Python-layer flag alone does not turn the sidecar routes on,
            same two-layer gating convention as enable_agent_pty/
            enable_node_interpreter/enable_browser_tool above. Completion
            only (no hover/signatureHelp/diagnostics push) and
            full-document sync only (every call resends the whole current
            file, no incremental didChange deltas) -- see the scoping doc
            for the explicit list of what's deferred.

    Returns:
        List of ToolSpec

    Example:
        ```python
        from boxkite.manager import SandboxManager
        from boxkite.tools import create_sandbox_tool_specs

        manager = SandboxManager()
        session = await manager.create_session(org_id, session_id, work_item_id)

        specs = create_sandbox_tool_specs(
            sandbox_manager=manager,
            organization_id=org_id,
            work_item_id=work_item_id,
            session_id=session_id,
        )
        bash_spec = next(s for s in specs if s.name == "bash_tool")
        result = await bash_spec.handler(command="echo hi")
        ```
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    effective_session_id = session_id or (
        str(lazy_runtime.session_id) if lazy_runtime is not None else None
    )

    # session_id may contain a prefix (e.g. "execution:<uuid>") — extract bare UUID for audit linkage
    session_id_uuid: Optional[UUID] = None
    if effective_session_id:
        bare_id = (
            effective_session_id.split(":", 1)[-1]
            if ":" in effective_session_id
            else effective_session_id
        )
        try:
            session_id_uuid = UUID(bare_id)
        except ValueError:
            logger.warning(
                f"[SandboxTools] Non-UUID session_id={effective_session_id!r}; "
                "AuditSink session linkage will be omitted where UUID is required"
            )

    logger.info(
        f"[SandboxTools] Creating tools for org={organization_id}, "
        f"work_item={work_item_id}, session={effective_session_id}, "
        f"mode={'lazy' if lazy_runtime is not None else 'eager'}"
    )

    specs = [
        # 1. Command execution (mirrors to AuditSink when configured)
        create_bash_tool_spec(
            session_id=effective_session_id,
            sandbox_manager=sandbox_manager,
            lazy_runtime=lazy_runtime,
            allowed_commands=allowed_commands,
            organization_id=organization_id,
            work_item_id=work_item_id,
            agent_name=agent_name,
            audit_sink=audit_sink,
            enable_secret_env=enable_secret_env,
        ),

        # 2. Persistent Python interpreter (mirrors to AuditSink when configured)
        create_python_interpreter_tool_spec(
            session_id=effective_session_id,
            sandbox_manager=sandbox_manager,
            lazy_runtime=lazy_runtime,
            organization_id=organization_id,
            work_item_id=work_item_id,
            agent_name=agent_name,
            audit_sink=audit_sink,
        ),

        # 3. File creation (mirrors to AuditSink when configured)
        create_file_create_tool_spec(
            organization_id=organization_id,
            work_item_id=work_item_id,
            session_id=effective_session_id,
            agent_name=agent_name,
            sandbox_manager=sandbox_manager,
            audit_sink=audit_sink,
            lazy_runtime=lazy_runtime,
        ),

        # 4. File viewing
        create_view_tool_spec(
            sandbox_manager=sandbox_manager,
            session_id=effective_session_id,
            lazy_runtime=lazy_runtime,
        ),

        # 5. File editing (mirrors to AuditSink when configured)
        create_str_replace_tool_spec(
            organization_id=organization_id,
            work_item_id=work_item_id,
            session_id=effective_session_id,
            agent_name=agent_name,
            sandbox_manager=sandbox_manager,
            audit_sink=audit_sink,
            lazy_runtime=lazy_runtime,
        ),

        # 6. Download URL generation (mirrors to AuditSink when configured)
        create_present_files_tool_spec(
            audit_sink=audit_sink,
            organization_id=organization_id,
            work_item_id=work_item_id,
            sandbox_manager=sandbox_manager,
            session_id=effective_session_id,
            chat_session_id=session_id_uuid,
            agent_name=agent_name,
            lazy_runtime=lazy_runtime,
        ),

        # 7. Directory listing (read-only)
        create_ls_tool_spec(
            sandbox_manager=sandbox_manager,
            session_id=effective_session_id,
            lazy_runtime=lazy_runtime,
        ),

        # 8. File search by name pattern (read-only)
        create_glob_tool_spec(
            sandbox_manager=sandbox_manager,
            session_id=effective_session_id,
            lazy_runtime=lazy_runtime,
        ),

        # 9. File content search by regex (read-only)
        create_grep_tool_spec(
            sandbox_manager=sandbox_manager,
            session_id=effective_session_id,
            lazy_runtime=lazy_runtime,
        ),

        # 10. Start a tracked background process
        create_start_process_tool_spec(
            sandbox_manager=sandbox_manager,
            session_id=effective_session_id,
            lazy_runtime=lazy_runtime,
            allowed_commands=allowed_commands,
        ),

        # 11. Poll a background process's output
        create_get_process_output_tool_spec(
            sandbox_manager=sandbox_manager,
            session_id=effective_session_id,
            lazy_runtime=lazy_runtime,
        ),

        # 12. Write to a background process's stdin
        create_send_process_input_tool_spec(
            sandbox_manager=sandbox_manager,
            session_id=effective_session_id,
            lazy_runtime=lazy_runtime,
        ),

        # 13. Stop a background process
        create_stop_process_tool_spec(
            sandbox_manager=sandbox_manager,
            session_id=effective_session_id,
            lazy_runtime=lazy_runtime,
        ),

        # 14. List tracked background processes
        create_list_processes_tool_spec(
            sandbox_manager=sandbox_manager,
            session_id=effective_session_id,
            lazy_runtime=lazy_runtime,
        ),

        # 15. Long-poll for a filesystem change (read-only, docs/FILE-WATCHER-DESIGN.md)
        create_watch_directory_tool_spec(
            sandbox_manager=sandbox_manager,
            session_id=effective_session_id,
            lazy_runtime=lazy_runtime,
        ),
    ]

    if enable_http_request_tool:
        # Secrets-broker HTTP request tool (opt-in, see enable_http_request_tool's
        # docstring above). Mirrors to AuditSink when configured -- method/url/
        # status only, never headers/body (may contain {{secret:...}} references
        # or, post-substitution server-side, values this process never sees).
        specs.append(
            create_http_request_tool_spec(
                session_id=effective_session_id,
                sandbox_manager=sandbox_manager,
                lazy_runtime=lazy_runtime,
                organization_id=organization_id,
                work_item_id=work_item_id,
                agent_name=agent_name,
                audit_sink=audit_sink,
            )
        )

    if enable_agent_pty:
        # Agent-callable PTY (opt-in, see enable_agent_pty's docstring above).
        # Also requires BOXKITE_AGENT_PTY_ENABLED=true server-side -- this
        # Python-layer flag alone does not turn the sidecar route on.
        # allowed_commands/audit_sink threaded through the same way
        # bash_tool gets them -- the #69 security review found pty_exec
        # previously bypassed the command whitelist entirely and wrote no
        # audit record at all; both are now enforced/wired identically to
        # bash_tool's own handling.
        specs.append(
            create_pty_exec_tool_spec(
                session_id=effective_session_id,
                sandbox_manager=sandbox_manager,
                lazy_runtime=lazy_runtime,
                allowed_commands=allowed_commands,
                organization_id=organization_id,
                work_item_id=work_item_id,
                agent_name=agent_name,
                audit_sink=audit_sink,
            )
        )

    if enable_node_interpreter:
        # Persistent Node.js interpreter (opt-in, see enable_node_interpreter's
        # docstring above). Also requires BOXKITE_NODE_INTERPRETER_ENABLED=true
        # server-side -- this Python-layer flag alone does not turn the
        # sidecar route on. Mirrors to AuditSink when configured, same as
        # python_interpreter.
        specs.append(
            create_node_interpreter_tool_spec(
                session_id=effective_session_id,
                sandbox_manager=sandbox_manager,
                lazy_runtime=lazy_runtime,
                organization_id=organization_id,
                work_item_id=work_item_id,
                agent_name=agent_name,
                audit_sink=audit_sink,
            )
        )

    if enable_run_tests:
        # run_tests (opt-in, see enable_run_tests's docstring above). Threads
        # allowed_commands the same way bash_tool/start_process do -- it
        # runs through the identical exec primitive, so it must be subject
        # to the same per-agent command whitelist.
        specs.append(
            create_run_tests_tool_spec(
                sandbox_manager=sandbox_manager,
                session_id=effective_session_id,
                lazy_runtime=lazy_runtime,
                allowed_commands=allowed_commands,
            )
        )

    if enable_browser_tool:
        # browser_navigate/browser_exec/browser_screenshot/browser_close
        # (opt-in, see enable_browser_tool's docstring above). Also requires
        # BOXKITE_BROWSER_ENABLED=true server-side -- this Python-layer flag
        # alone does not turn the sidecar routes on. Mirrors to AuditSink
        # when configured (never a screenshot's image bytes -- see
        # browser_tools.py's own module docstring).
        specs.extend(
            create_browser_tool_specs(
                session_id=effective_session_id,
                sandbox_manager=sandbox_manager,
                lazy_runtime=lazy_runtime,
                organization_id=organization_id,
                work_item_id=work_item_id,
                agent_name=agent_name,
                audit_sink=audit_sink,
            )
        )

    if enable_lsp_tools:
        # lsp_start/lsp_completion/lsp_stop (opt-in, see enable_lsp_tools's
        # docstring above). Also requires BOXKITE_LSP_ENABLED=true
        # server-side -- this Python-layer flag alone does not turn the
        # sidecar routes on. lsp_completion mirrors to AuditSink when
        # configured (the one "exec-like" moment of this feature);
        # lsp_start/lsp_stop don't, same as start_process/stop_process.
        specs.extend(
            create_lsp_tool_specs(
                session_id=effective_session_id,
                sandbox_manager=sandbox_manager,
                lazy_runtime=lazy_runtime,
                organization_id=organization_id,
                work_item_id=work_item_id,
                agent_name=agent_name,
                audit_sink=audit_sink,
            )
        )

    if enable_git_tools:
        # 15-22. Git operations (opt-in; git_clone/git_commit/git_push mirror
        # to AuditSink when configured -- see git_tools.py's module
        # docstring for the credential-handling and network-egress caveats).
        specs.extend(
            create_git_tool_specs(
                sandbox_manager=sandbox_manager,
                session_id=effective_session_id,
                lazy_runtime=lazy_runtime,
                audit_sink=audit_sink,
                organization_id=organization_id,
                work_item_id=work_item_id,
                agent_name=agent_name,
            )
        )

    logger.info(f"[SandboxTools] Created {len(specs)} tools: {[s.name for s in specs]}")

    return specs


def create_sandbox_tools(
    sandbox_manager: Optional['SandboxManager'] = None,
    audit_sink: Optional[AuditSink] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    session_id: Optional[str] = None,
    agent_name: Optional[str] = None,
    llm_client: Optional[Any] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
    allowed_commands: Optional[list] = None,
    enable_git_tools: bool = False,
    enable_http_request_tool: bool = False,
    enable_secret_env: bool = False,
    enable_agent_pty: bool = False,
    enable_node_interpreter: bool = False,
    enable_run_tests: bool = False,
    enable_browser_tool: bool = False,
    enable_lsp_tools: bool = False,
) -> list:
    """
    Create all sandbox tools for an agent, as LangChain tools.

    Backward-compatible wrapper: builds the same ToolSpecs as
    `create_sandbox_tool_specs()` (see its docstring for the full tool
    list and argument descriptions) and adapts them into LangChain tools
    via boxkite.tools.adapters.to_langchain_tools. Requires the
    `langchain` extra (`pip install boxkite-sandbox[langchain]`).

    Prefer `create_sandbox_tool_specs()` for new code, or if you're not
    using LangChain/LangGraph — it has no LangChain dependency at all.

    Returns:
        List of LangChain tools

    Example:
        ```python
        from boxkite.manager import SandboxManager
        from boxkite.tools import create_sandbox_tools

        # Create manager and session
        manager = SandboxManager()
        session = await manager.create_session(org_id, session_id, work_item_id)

        # Create tools (no AuditSink — storage alone is enough to work with)
        sandbox_tools = create_sandbox_tools(
            sandbox_manager=manager,
            organization_id=org_id,
            work_item_id=work_item_id,
            session_id=session_id,
        )
        ```
    """
    from .adapters import to_langchain_tools

    specs = create_sandbox_tool_specs(
        sandbox_manager=sandbox_manager,
        audit_sink=audit_sink,
        organization_id=organization_id,
        work_item_id=work_item_id,
        session_id=session_id,
        agent_name=agent_name,
        llm_client=llm_client,
        lazy_runtime=lazy_runtime,
        allowed_commands=allowed_commands,
        enable_git_tools=enable_git_tools,
        enable_http_request_tool=enable_http_request_tool,
        enable_secret_env=enable_secret_env,
        enable_agent_pty=enable_agent_pty,
        enable_node_interpreter=enable_node_interpreter,
        enable_run_tests=enable_run_tests,
        enable_browser_tool=enable_browser_tool,
        enable_lsp_tools=enable_lsp_tools,
    )
    return to_langchain_tools(specs)


def create_sandbox_tools_with_manager(
    sandbox_manager: 'SandboxManager',
    session_id: str,
    audit_sink: Optional[AuditSink] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    llm_client: Optional[Any] = None,
) -> list:
    """
    Convenience function to create sandbox tools (as LangChain tools) with
    session_id as string.

    Args:
        sandbox_manager: SandboxManager instance (required)
        session_id: Session ID as string (required)
        audit_sink: Optional AuditSink (see ../audit.py), fully optional
        organization_id: Organization ID (optional, for AuditSink)
        work_item_id: Work item ID (optional, for AuditSink)
        agent_name: Agent name for audit trail
        llm_client: LLM client for vision image analysis (optional)

    Returns:
        List of LangChain tools
    """
    return create_sandbox_tools(
        sandbox_manager=sandbox_manager,
        audit_sink=audit_sink,
        organization_id=organization_id,
        work_item_id=work_item_id,
        session_id=session_id,
        agent_name=agent_name,
        llm_client=llm_client,
    )
