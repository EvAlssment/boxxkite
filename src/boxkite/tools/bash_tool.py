"""
Bash Tool - Execute commands in sandbox

Replaces execute_python, execute_bash, execute_javascript with a single
unified tool. Agents use bash to run any command:
- Python: python3 -c "code" or python3 script.py
- Node.js: node -e "code" or node script.js
- Shell: ls, cat, grep, jq, etc.

SECURITY:
- Network is disabled (--network=none)
- pip/npm install commands are blocked
- Only preset packages are available
- Environment variables are sanitized (no API keys)

Execution happens via SandboxManager HTTP calls to the sidecar container.

This module is framework-agnostic: `create_bash_tool_spec()` returns a
plain `ToolSpec` (see ./types.py) whose handler is a normal async callable
with no LangChain import anywhere in this file. `create_bash_tool()` is a
backward-compatible wrapper that adapts that spec into a LangChain tool
(see ./adapters.py) for existing callers.
"""

import logging
import re
import time
from typing import Optional, TYPE_CHECKING
from uuid import UUID

from ..audit import AuditSink, safe_call
from ..preset_packages import is_blocked_command, get_module_not_found_message
from ..command_whitelist import validate_command_whitelist
from ..lazy_runtime import resolve_sandbox_operation_context
from .types import ToolSpec

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

logger = logging.getLogger(__name__)

# Patterns to redact from output (in case something slips through)
SENSITIVE_OUTPUT_PATTERNS = [
    # API keys and tokens
    (r'(api[_-]?key|apikey|api_secret|secret_key|access_token|auth_token)["\s:=]+["\']?[\w-]{20,}', '[REDACTED_KEY]'),
    # AWS credentials
    (r'AKIA[0-9A-Z]{16}', '[REDACTED_AWS_KEY]'),
    (r'aws_secret_access_key["\s:=]+["\']?[\w/+=]{40}', '[REDACTED_AWS_SECRET]'),
    # Azure connection strings
    (r'AccountKey=[\w/+=]+', 'AccountKey=[REDACTED]'),
    (r'DefaultEndpointsProtocol=https;AccountName=\w+;AccountKey=[\w/+=]+', '[REDACTED_AZURE_CONN_STRING]'),
    # Generic secrets
    (r'(password|passwd|pwd)["\s:=]+["\']?[^\s"\']{8,}', r'\1=[REDACTED]'),
    # JWT tokens
    (r'eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+', '[REDACTED_JWT]'),
    # GitHub tokens (personal access, OAuth, user-to-server, server-to-server, refresh)
    (r'gh[pousr]_[A-Za-z0-9]{36}', '[REDACTED_GITHUB_TOKEN]'),
    # GitLab personal access tokens
    (r'glpat-[A-Za-z0-9_-]{20}', '[REDACTED_GITLAB_TOKEN]'),
    # SSH private keys (PEM and OpenSSH formats) -- defense-in-depth backstop
    # for docs/GIT-OPERATIONS-DESIGN.md's git tool set (see git_tools.py):
    # the primary control there is "never write to a synced path, delete
    # same-call," not this pattern, per the design doc's §5 explicit
    # warning against relying on output redaction as the primary control.
    (r'-----BEGIN (?:OPENSSH|RSA|EC|DSA|ENCRYPTED) PRIVATE KEY-----[\s\S]+?-----END (?:OPENSSH|RSA|EC|DSA|ENCRYPTED) PRIVATE KEY-----', '[REDACTED_SSH_PRIVATE_KEY]'),
    # Generic self-hosted/Bitbucket-style personal access tokens that don't
    # match a vendor-specific prefix above (best-effort, high false-negative
    # rate by design -- narrow enough to avoid flagging ordinary hex hashes).
    (r'\b[A-Za-z0-9]{4,10}_[A-Za-z0-9]{20,}\b', '[REDACTED_PAT]'),
]


def sanitize_output(output: str) -> str:
    """
    Sanitize command output to redact any sensitive information.

    Args:
        output: Raw command output

    Returns:
        Sanitized output with sensitive patterns redacted
    """
    sanitized = output
    for pattern, replacement in SENSITIVE_OUTPUT_PATTERNS:
        sanitized = re.sub(pattern, replacement, sanitized, flags=re.IGNORECASE)
    return sanitized


BASH_TOOL_DESCRIPTION = """Execute a bash command in the sandboxed environment.

Use this tool to run any command - Python code, Node.js, shell utilities, etc.

**File System Paths:**
- /workspace - Working files (synced to storage, shown in Files UI)
- /mnt/user-data/outputs - Deliverables for users (synced, shown in Files UI)
- /mnt/user-data/uploads - User uploads (read-only)
- /tmp - Ephemeral scratch space (NOT synced, for throwaway scripts)

**Examples:**
- Python: `python3 -c "import pandas as pd; print(pd.__version__)"`
- Python script: `python3 /workspace/analysis.py`
- Temp script: `echo 'print("hello")' > /tmp/test.py && python3 /tmp/test.py`
- Node.js: `node -e "console.log(JSON.stringify({hello: 'world'}))"`
- Shell: `ls -la /workspace`
- Data (upload): `jq '.data[]' /mnt/user-data/uploads/input.json`

**Pre-installed Python packages:**
pandas, numpy, polars, matplotlib, seaborn, plotly, scipy, scikit-learn,
openpyxl, xlrd, xlsxwriter, python-docx, python-pptx, pypdf, pdfplumber,
reportlab, pypdfium2, pdf2image, pytesseract, markitdown, beautifulsoup4,
pillow, and more.

**Pre-installed Node.js packages:**
docx, pptxgenjs, skia-canvas, linebreak, fontkit, prismjs, mathjax-full,
sharp, playwright, pdf-lib, pdfjs-dist, react, react-dom, react-icons.

**Pre-installed system tools:**
python3, node, jq, sqlite3, libreoffice/soffice, poppler tools
(pdftoppm, pdftocairo, pdfinfo), qpdf, pandoc, tesseract, ImageMagick
(magick/convert), ghostscript (gs), heif-convert

**IMPORTANT:**
- Package installation (pip install, npm install) is NOT available
- Network access is disabled
- Use only the pre-installed packages listed above
- Use /tmp for throwaway scripts that don't need to persist

Args:
    command: Bash command to execute
    timeout: Timeout in seconds (default 120, max 600)

Returns:
    Command output (stdout + stderr)
"""

BASH_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": "Bash command to execute",
        },
        "timeout": {
            "type": "integer",
            "description": "Timeout in seconds (default 120, max 600)",
            "default": 120,
        },
    },
    "required": ["command"],
}


def create_bash_tool_spec(
    session_id: Optional[str] = None,
    sandbox_manager: Optional['SandboxManager'] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
    allowed_commands: Optional[list] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
    enable_secret_env: bool = False,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for bash_tool.

    If an AuditSink is provided, executed commands are also mirrored there
    (see ../audit.py) — this is optional and never blocks command execution.

    Args:
        session_id: Session ID for tracking
        sandbox_manager: SandboxManager instance (required unless lazy_runtime is provided)
        allowed_commands: Optional per-agent command allowlist. When set
            (non-empty), only these program names may appear in command
            positions — see command_whitelist.validate_command_whitelist.
        organization_id: Organization ID (for audit trail)
        work_item_id: Work item ID (for audit trail)
        agent_name: Optional agent name for audit trail
        audit_sink: Optional AuditSink to mirror executed commands into an
            external system
        enable_secret_env: Opt in to the `secret_env` parameter
            (docs/SECRETS-DESIGN.md's bash_tool addendum) -- a
            {env_var_name: granted_secret_name} mapping the agent supplies
            by NAME only, resolved server-side by the sidecar into the
            spawned process's environment, never into the command string
            and never a literal value the agent itself provides. Off by
            default, same "new credential-consumption surface is an
            explicit opt-in" convention as
            create_sandbox_tool_specs(enable_http_request_tool=...). When
            False, a `secret_env` argument passed by an agent is silently
            ignored (not an error) rather than exposed in the tool schema
            at all.

    Returns:
        ToolSpec with a plain async handler(command, timeout, secret_env=None) -> str
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    canonical_session_id = str(session_id).strip() if session_id else None
    session_id_uuid: Optional[UUID] = None
    if canonical_session_id:
        bare_id = (
            canonical_session_id.split(":", 1)[1]
            if ":" in canonical_session_id
            else canonical_session_id
        )
        try:
            session_id_uuid = UUID(bare_id)
        except ValueError:
            logger.warning(
                f"[bash_tool] Non-UUID session_id={canonical_session_id!r}; "
                "AuditSink session linkage will be omitted where UUID is required"
            )
            session_id_uuid = None

    async def bash_tool(command: str, timeout: int = 120, secret_env: Optional[dict] = None) -> str:
        # Validate command is not empty
        if not command or not command.strip():
            return "Error: Empty command provided"

        # SECURITY: secret_env is only ever forwarded when this tool was
        # explicitly built with enable_secret_env=True -- an agent passing
        # this argument to a bash_tool instance that didn't opt in is
        # silently ignored, not an error, matching enable_http_request_tool's
        # own "the surface simply isn't there" gating rather than a runtime
        # permission check that could be probed.
        effective_secret_env = secret_env if (enable_secret_env and secret_env) else None

        # SECURITY: Per-agent command whitelist (when configured, only the
        # allowed program names may appear in any command position)
        if allowed_commands:
            is_allowed, whitelist_error = validate_command_whitelist(command, allowed_commands)
            if not is_allowed:
                logger.warning(f"[bash_tool] Whitelist blocked command: {command[:100]}")
                return whitelist_error

        # SECURITY: Check for blocked commands (package installation attempts)
        is_blocked, error_message = is_blocked_command(command)
        if is_blocked:
            logger.warning(f"[bash_tool] Blocked command attempt: {command[:100]}")
            return error_message

        # Clamp timeout
        effective_timeout = min(max(1, timeout), 600)
        if timeout > 600:
            logger.warning(f"[bash_tool] Clamping timeout from {timeout}s to 600s")

        logger.info(f"[bash_tool] Executing: {command[:200]}...")

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            # Execute via SandboxManager HTTP API
            start_time = time.monotonic()
            result = await resolved_manager.execute(
                session_id=resolved_session_id,
                command=command,
                timeout=effective_timeout,
                secret_env=effective_secret_env,
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)
            exit_code = result.get("exit_code", 0)
            stdout = result.get("stdout", "")
            stderr = result.get("stderr", "")
            output = stdout
            if stderr:
                output = f"{stdout}\n{stderr}" if stdout else stderr

            # Mirror to the optional AuditSink (no-op if not configured)
            if audit_sink:
                await safe_call(
                    audit_sink,
                    "record_exec",
                    organization_id=organization_id,
                    work_item_id=work_item_id,
                    session_id=str(session_id_uuid) if session_id_uuid else canonical_session_id,
                    agent_name=agent_name,
                    command=command,
                    exit_code=exit_code,
                    duration_ms=duration_ms,
                )

            # SECURITY: Sanitize output to redact any sensitive information
            output = sanitize_output(output)

            # Check for module not found errors and provide helpful message
            if exit_code != 0:
                output_lower = output.lower()

                # Check for Python module errors
                module_match = re.search(
                    r"no module named ['\"]?(\w+)['\"]?",
                    output,
                    re.IGNORECASE
                )
                if module_match or "modulenotfounderror" in output_lower:
                    module_name = module_match.group(1) if module_match else "unknown"
                    return (
                        f"Error (exit code {exit_code}):\n{output}\n\n"
                        f"{get_module_not_found_message(module_name)}"
                    )

                # Check for Node.js module errors
                node_module_match = re.search(
                    r"cannot find module ['\"]([^'\"]+)['\"]",
                    output,
                    re.IGNORECASE,
                )
                if node_module_match:
                    module_name = node_module_match.group(1)
                    return (
                        f"Error (exit code {exit_code}):\n{output}\n\n"
                        f"{get_module_not_found_message(module_name)}"
                    )

                # Check for command not found
                if "command not found" in output_lower or "not found" in output_lower:
                    return (
                        f"Error (exit code {exit_code}):\n{output}\n\n"
                        f"The command may not be available in the sandbox.\n"
                        f"Available tools include python3, node, jq, sqlite3, "
                        f"libreoffice/soffice, poppler tools, qpdf, pandoc, "
                        f"tesseract, ImageMagick, ghostscript, heif-convert, "
                        f"and standard Unix commands."
                    )

                return f"Error (exit code {exit_code}):\n{output}"

            # Success
            if output:
                return output
            else:
                return "(command completed with no output)"

        except Exception as e:
            logger.error(f"[bash_tool] Execution error: {e}", exc_info=True)
            return f"Error executing command: {str(e)}"

    parameters = BASH_TOOL_PARAMETERS
    if enable_secret_env:
        parameters = {
            **BASH_TOOL_PARAMETERS,
            "properties": {
                **BASH_TOOL_PARAMETERS["properties"],
                "secret_env": {
                    "type": "object",
                    "description": (
                        "Optional {env_var_name: granted_secret_name} mapping -- "
                        "reference one of this session's already-granted secrets "
                        "BY NAME (never a literal value) to have it injected into "
                        "this command's own process environment. E.g. "
                        '{"ANTHROPIC_API_KEY": "claude-code-key"} if "claude-code-key" '
                        "is a secret already granted to this session."
                    ),
                },
            },
        }

    return ToolSpec(
        name="bash_tool",
        description=BASH_TOOL_DESCRIPTION,
        parameters=parameters,
        handler=bash_tool,
    )


def create_bash_tool(
    session_id: Optional[str] = None,
    sandbox_manager: Optional['SandboxManager'] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
    allowed_commands: Optional[list] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
    enable_secret_env: bool = False,
):
    """
    Create the bash_tool as a LangChain tool (backward-compatible wrapper).

    Prefer `create_bash_tool_spec()` for framework-agnostic use — this
    function just adapts that spec via boxkite.tools.adapters.to_langchain_tools,
    kept for existing callers that expect a LangChain BaseTool directly.
    Requires the `langchain` extra (`pip install boxkite-sandbox[langchain]`).

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_bash_tool_spec(
        session_id=session_id,
        sandbox_manager=sandbox_manager,
        lazy_runtime=lazy_runtime,
        allowed_commands=allowed_commands,
        organization_id=organization_id,
        work_item_id=work_item_id,
        agent_name=agent_name,
        audit_sink=audit_sink,
        enable_secret_env=enable_secret_env,
    )
    return to_langchain_tools([spec])[0]
