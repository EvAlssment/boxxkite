"""Run Tests Tool -- run a test command and parse its output into a
structured, framework-agnostic schema (docs/issue #123).

Distinct from bash_tool: bash_tool hands back raw stdout/stderr for the
agent to re-parse itself every time; run_tests runs the exact same exec
primitive (SandboxManager.execute, same command whitelist and blocked-
command checks bash_tool applies) but then parses the combined output into
boxkite.tools.test_parsers.schema.TestRunResult and returns that as JSON --
`{framework, parsed, exit_code, passed, failed, errors, failures, duration_seconds}`.
See test_parsers/schema.py for the full field-by-field meaning and
test_parsers/registry.py for how a framework's output is matched to a
parser (only pytest is implemented so far; jest/go-test/cargo-test can be
added there later without changing this file or the schema).

When the framework isn't recognized, or its output doesn't match the
parser's expected shape, this returns `parsed: false` plus the full
`raw_output` instead of raising -- never a hard failure, per issue #123's
explicit requirement.

This module is framework-agnostic: `create_run_tests_tool_spec()` returns a
plain `ToolSpec` (see ./types.py) whose handler is a normal async callable
with no LangChain import anywhere in this file. `create_run_tests_tool()`
is a backward-compatible wrapper that adapts that spec into a LangChain
tool (see ./adapters.py) for existing callers.
"""

import json
import logging
from typing import Optional, TYPE_CHECKING

from ..command_whitelist import validate_command_whitelist
from ..lazy_runtime import resolve_sandbox_operation_context
from ..preset_packages import is_blocked_command
from .bash_tool import sanitize_output
from .test_parsers import SUPPORTED_FRAMEWORKS, detect_framework, get_parser
from .types import ToolSpec

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT_SECONDS = 300
MAX_TIMEOUT_SECONDS = 600


RUN_TESTS_DESCRIPTION = """
Run a test command and get back structured, machine-parseable results
instead of raw stdout to re-parse yourself.

Use this instead of bash_tool when running a project's test suite --
the response is JSON: {framework, parsed, exit_code, passed, failed,
errors, failures, duration_seconds}, where each entry in `failures` is
{file, line, name, message}.

Only pytest output is parsed today (`framework: "pytest"`, auto-detected
from `command` when not passed explicitly). Other frameworks still run
fine -- you just get `parsed: false` with the full raw output attached
instead of itemized failures, same as pytest output that doesn't match
the expected shape (e.g. a custom reporter plugin). Either way this
never fails hard just because parsing didn't work.

Args:
    command: The test command to run, e.g. "pytest tests/ -v"
    framework: Optional explicit framework name (currently only "pytest"
        has a parser). Leave unset to auto-detect from `command`.
    timeout: Timeout in seconds (default 300, max 600)

Returns:
    JSON string of the structured result (see above), or an error message
    if the command itself could not be run at all
"""

RUN_TESTS_PARAMETERS = {
    "type": "object",
    "properties": {
        "command": {
            "type": "string",
            "description": 'The test command to run, e.g. "pytest tests/ -v"',
        },
        "framework": {
            "type": "string",
            "description": (
                "Optional explicit framework name (currently only \"pytest\" "
                "has a parser). Leave unset to auto-detect from `command`."
            ),
        },
        "timeout": {
            "type": "integer",
            "description": f"Timeout in seconds (default {DEFAULT_TIMEOUT_SECONDS}, max {MAX_TIMEOUT_SECONDS})",
            "default": DEFAULT_TIMEOUT_SECONDS,
        },
    },
    "required": ["command"],
}


def create_run_tests_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    allowed_commands: Optional[list] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for run_tests.

    Args:
        sandbox_manager: SandboxManager instance (required, or lazy_runtime)
        session_id: Session ID for tracking
        lazy_runtime: Lazy sandbox runtime (required, or sandbox_manager)
        allowed_commands: Optional per-agent command allowlist -- mirrors
            bash_tool's own enforcement (see
            command_whitelist.validate_command_whitelist) so this tool
            cannot be used to bypass an agent's command restriction just
            because it runs `command` through the same exec primitive.

    Returns:
        ToolSpec with a plain async handler(command, framework, timeout) -> str
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def run_tests(
        command: str,
        framework: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> str:
        if not command or not command.strip():
            return "Error: Empty command provided"

        # SECURITY: same two checks bash_tool applies -- this tool is a
        # thin wrapper around the identical exec primitive, so it must not
        # become a way to run an otherwise-blocked/non-whitelisted command.
        if allowed_commands:
            is_allowed, whitelist_error = validate_command_whitelist(command, allowed_commands)
            if not is_allowed:
                logger.warning(f"[run_tests] Whitelist blocked command: {command[:100]}")
                return whitelist_error

        is_blocked, blocked_message = is_blocked_command(command)
        if is_blocked:
            logger.warning(f"[run_tests] Blocked command attempt: {command[:100]}")
            return blocked_message

        effective_timeout = min(max(1, timeout), MAX_TIMEOUT_SECONDS)
        effective_framework = (framework or detect_framework(command)).strip().lower()

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            result = await resolved_manager.execute(
                session_id=resolved_session_id,
                command=command,
                timeout=effective_timeout,
            )
        except Exception as e:
            logger.error(f"[run_tests] Error executing command: {e}", exc_info=True)
            return f"Error running tests: {str(e)}"

        exit_code = result.get("exit_code", 0)
        stdout = result.get("stdout", "")
        stderr = result.get("stderr", "")
        combined_output = sanitize_output(f"{stdout}\n{stderr}" if stderr else stdout)

        return json.dumps(
            _build_result_dict(effective_framework, combined_output, exit_code),
            indent=2,
        )

    return ToolSpec(
        name="run_tests",
        description=RUN_TESTS_DESCRIPTION,
        parameters=RUN_TESTS_PARAMETERS,
        handler=run_tests,
    )


def _build_result_dict(framework: str, combined_output: str, exit_code: int) -> dict:
    """Parse `combined_output` with `framework`'s parser, falling back to
    raw output (never raising) when unsupported or unparseable."""
    parser = get_parser(framework)
    if parser is not None:
        try:
            # dataclasses.asdict (via TestRunResult.to_dict) recursively
            # converts the nested TestFailure list into plain dicts too.
            return parser(combined_output, exit_code).to_dict()
        except ValueError as e:
            logger.info(f"[run_tests] {framework} parser could not parse output, falling back to raw: {e}")

    return {
        "framework": framework,
        "parsed": False,
        "exit_code": exit_code,
        "passed": 0,
        "failed": 0,
        "errors": 0,
        "failures": [],
        "duration_seconds": None,
        "raw_output": combined_output,
        "note": (
            f"No parser available for framework={framework!r} (supported: "
            f"{', '.join(SUPPORTED_FRAMEWORKS)}); returning raw output."
            if framework not in SUPPORTED_FRAMEWORKS
            else None
        ),
    }


def create_run_tests_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    allowed_commands: Optional[list] = None,
):
    """Create the run_tests tool as a LangChain tool (backward-compatible wrapper).

    Prefer `create_run_tests_tool_spec()` for framework-agnostic use.
    Requires the `langchain` extra.

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_run_tests_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
        allowed_commands=allowed_commands,
    )
    return to_langchain_tools([spec])[0]
