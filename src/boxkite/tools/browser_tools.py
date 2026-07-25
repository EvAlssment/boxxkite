"""
Browser Tools - headless Chromium automation (Playwright/CDP) --
docs/BROWSER-EXEC-DESIGN.md, GitHub issue #119.

Four narrow primitives against ONE lazily-started, kept-alive headless
Chromium process per session (the sidecar's /browser/* routes,
sidecar/sidecar_browser.py) -- browser_navigate loads a URL, browser_exec
evaluates a script in the current page's JS context (Playwright's
page.evaluate / CDP Runtime.evaluate), browser_screenshot captures the
current page as a PNG, browser_close tears the process down. See the
design doc §2 for why this is four narrow primitives rather than a bare
`browser_exec(playwright_script)` that would accept an arbitrary
Playwright script string.

Off by default at TWO layers, not one, matching pty_tools.py's/
node_interpreter_tool.py's convention for new attack surface: these tools
are only wired into create_sandbox_tool_specs() when the caller passes
enable_browser_tool=True, AND the sidecar itself 404s every /browser/*
call unless BOXKITE_BROWSER_ENABLED is set server-side -- this Python-layer
flag alone does not turn the routes on.

SECURITY (docs/BROWSER-EXEC-DESIGN.md §3): this is the genuinely riskiest
opt-in tool this repo ships. Every other egress-needing tool here either
needs no network at all, or needs egress to a fixed, enumerable set of
hosts known at session-creation time -- a browser's entire point is going
to a URL nobody enumerated in advance, so its NetworkPolicy (see
src/boxkite/browser_network_policy.py) must allow broad HTTPS/DNS egress,
with an unconditional, higher-priority deny for link-local/RFC1918/
loopback ranges as the only thing keeping this from being unrestricted
internet access. There is no sidecar-side per-request destination check in
this path the way there is for http_request_tool -- the browser process
IS the HTTP client, resolving DNS and opening sockets itself.

This module is framework-agnostic: each `create_browser_*_tool_spec()`
returns a plain `ToolSpec` (see ./types.py) whose handler is a normal async
callable with no LangChain import anywhere in this file.
"""

import logging
import time
from typing import Optional, TYPE_CHECKING
from uuid import UUID

from ..audit import AuditSink, safe_call
from ..lazy_runtime import resolve_sandbox_operation_context
from .types import ToolImageResult, ToolSpec

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

logger = logging.getLogger(__name__)

_ALLOWED_WAIT_UNTIL = ("load", "domcontentloaded", "networkidle", "commit")

BROWSER_NAVIGATE_DESCRIPTION = """
Load a URL in the session's one headless-Chromium browser page.

Starts the browser automatically on first use -- no separate "start
browser" call needed. There is exactly one page per session; navigating
again replaces what was previously loaded. May not be available (404) if
this deployment hasn't enabled it.

A page that fails to load (bad host, DNS failure, timeout) is reported via
the `error` field, not by raising -- title/url/status will be empty/None
in that case.

Args:
    url: URL to navigate to (must include a scheme, e.g. "https://...")
    wait_until: One of "load" (default), "domcontentloaded", "networkidle",
        "commit"
    timeout_seconds: How long to wait for the navigation (default 30)

Returns:
    The resulting page's title, url, and HTTP status, or an error
"""

BROWSER_NAVIGATE_PARAMETERS = {
    "type": "object",
    "properties": {
        "url": {"type": "string", "description": "URL to navigate to"},
        "wait_until": {
            "type": "string",
            "description": "One of load, domcontentloaded, networkidle, commit",
            "default": "load",
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "Timeout in seconds (default 30)",
            "default": 30,
        },
    },
    "required": ["url"],
}

BROWSER_EXEC_DESCRIPTION = """
Evaluate JavaScript in the current browser page's JS context.

Use this to read page state (document.title, querySelector text/attributes,
computed layout) or drive basic interaction (el.click(), setting an
input's .value plus dispatching a synthetic event). This is page-context
script execution against whatever the loaded page itself allows (DOM
access, fetch subject to the page's own CSP/CORS) -- NOT host code
execution, and NOT a persistent variable scope across separate calls the
way node_interpreter is (each call is a fresh page.evaluate against
whatever page is currently loaded).

Returns the script's last-expression value (same completion-value
semantics a browser devtools console has), JSON-serialized. A thrown
exception is reported via the `error` field, not by raising.

Args:
    script: JavaScript to evaluate in the current page
    timeout_seconds: Timeout in seconds (default 10)

Returns:
    The script's JSON-serializable result, or an error
"""

BROWSER_EXEC_PARAMETERS = {
    "type": "object",
    "properties": {
        "script": {
            "type": "string",
            "description": "JavaScript to evaluate in the current page's context",
        },
        "timeout_seconds": {
            "type": "integer",
            "description": "Timeout in seconds (default 10)",
            "default": 10,
        },
    },
    "required": ["script"],
}

BROWSER_SCREENSHOT_DESCRIPTION = """
Capture the current browser page as an image.

Use this when you need a picture rather than DOM state -- confirming
rendered layout, describing a CAPTCHA back to the caller, or visually
confirming a form actually submitted.

Args:
    full_page: Capture the full scrollable page rather than just the
        current viewport (default False)

Returns:
    The captured image (multimodal), or an error string if the capture
    failed (e.g. the page is too large -- try full_page=False)
"""

BROWSER_SCREENSHOT_PARAMETERS = {
    "type": "object",
    "properties": {
        "full_page": {
            "type": "boolean",
            "description": "Capture the full scrollable page instead of just the viewport",
            "default": False,
        },
    },
    "required": [],
}

BROWSER_CLOSE_DESCRIPTION = """
Tear down the session's browser process.

Idempotent -- a no-op if no browser is currently running. The next
browser_navigate call starts a completely fresh browser (no cookies,
history, or page state carried over).

Returns:
    Confirmation that the browser was closed
"""

BROWSER_CLOSE_PARAMETERS = {"type": "object", "properties": {}, "required": []}


def _bare_session_id_uuid(session_id: Optional[str]) -> Optional[UUID]:
    canonical_session_id = str(session_id).strip() if session_id else None
    if not canonical_session_id:
        return None
    bare_id = (
        canonical_session_id.split(":", 1)[1]
        if ":" in canonical_session_id
        else canonical_session_id
    )
    try:
        return UUID(bare_id)
    except ValueError:
        logger.warning(
            f"[browser_tools] Non-UUID session_id={canonical_session_id!r}; "
            "AuditSink session linkage will be omitted where UUID is required"
        )
        return None


def create_browser_navigate_tool_spec(
    session_id: Optional[str] = None,
    sandbox_manager: Optional["SandboxManager"] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for browser_navigate.

    Mirrors create_node_interpreter_tool_spec's shape -- see its docstring
    for the AuditSink integration details, which are identical here. Per
    docs/BROWSER-EXEC-DESIGN.md §6, the navigated URL is recorded (never a
    screenshot's image bytes), same "don't put payload content in the
    audit trail beyond what's needed to reconstruct what happened"
    restraint http_request_tool.py's own docstring states.
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    session_id_uuid = _bare_session_id_uuid(session_id)

    async def browser_navigate(
        url: str, wait_until: str = "load", timeout_seconds: int = 30
    ) -> str:
        if not url or not url.strip():
            return "Error: Empty url provided"
        if wait_until not in _ALLOWED_WAIT_UNTIL:
            return f"Error: wait_until must be one of {list(_ALLOWED_WAIT_UNTIL)}"

        logger.info(f"[browser_navigate] {url[:200]}")

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            start_time = time.monotonic()
            result = await resolved_manager.browser_navigate(
                session_id=resolved_session_id,
                url=url,
                wait_until=wait_until,
                timeout_seconds=timeout_seconds,
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)

            error = result.get("error")

            if audit_sink:
                await safe_call(
                    audit_sink,
                    "record_exec",
                    organization_id=organization_id,
                    work_item_id=work_item_id,
                    session_id=str(session_id_uuid) if session_id_uuid else session_id,
                    agent_name=agent_name,
                    command=f"browser_navigate({url})",
                    exit_code=1 if error else 0,
                    duration_ms=duration_ms,
                )

            if error:
                return f"Error: {error}"

            return (
                f"title: {result.get('title')}\n"
                f"url: {result.get('url')}\n"
                f"status: {result.get('status')}"
            )

        except Exception as e:
            logger.error(f"[browser_navigate] Error: {e}", exc_info=True)
            return f"Error navigating: {str(e)}"

    return ToolSpec(
        name="browser_navigate",
        description=BROWSER_NAVIGATE_DESCRIPTION,
        parameters=BROWSER_NAVIGATE_PARAMETERS,
        handler=browser_navigate,
    )


def create_browser_exec_tool_spec(
    session_id: Optional[str] = None,
    sandbox_manager: Optional["SandboxManager"] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for browser_exec.

    Mirrors create_browser_navigate_tool_spec's shape -- see its docstring
    for the AuditSink integration details, which are identical here.
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    session_id_uuid = _bare_session_id_uuid(session_id)

    async def browser_exec(script: str, timeout_seconds: int = 10) -> str:
        if not script or not script.strip():
            return "Error: Empty script provided"

        logger.info(f"[browser_exec] {script[:200]}")

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            start_time = time.monotonic()
            result = await resolved_manager.browser_exec(
                session_id=resolved_session_id,
                script=script,
                timeout_seconds=timeout_seconds,
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)

            error = result.get("error")

            if audit_sink:
                await safe_call(
                    audit_sink,
                    "record_exec",
                    organization_id=organization_id,
                    work_item_id=work_item_id,
                    session_id=str(session_id_uuid) if session_id_uuid else session_id,
                    agent_name=agent_name,
                    command=script,
                    exit_code=1 if error else 0,
                    duration_ms=duration_ms,
                )

            if error:
                return f"Error: {error}"

            return str(result.get("result"))

        except Exception as e:
            logger.error(f"[browser_exec] Error: {e}", exc_info=True)
            return f"Error executing script: {str(e)}"

    return ToolSpec(
        name="browser_exec",
        description=BROWSER_EXEC_DESCRIPTION,
        parameters=BROWSER_EXEC_PARAMETERS,
        handler=browser_exec,
    )


def create_browser_screenshot_tool_spec(
    session_id: Optional[str] = None,
    sandbox_manager: Optional["SandboxManager"] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for browser_screenshot.

    Returns a framework-agnostic ToolImageResult on success (same
    multimodal-output pattern file_tools.py's `view` tool uses for image
    paths -- see ./types.py's ToolSpec.returns_multimodal), or a plain
    error string on failure. Per docs/BROWSER-EXEC-DESIGN.md §6, the audit
    record never includes the image bytes themselves.
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    session_id_uuid = _bare_session_id_uuid(session_id)

    async def browser_screenshot(full_page: bool = False):
        logger.info(f"[browser_screenshot] full_page={full_page}")

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            start_time = time.monotonic()
            result = await resolved_manager.browser_screenshot(
                session_id=resolved_session_id,
                full_page=full_page,
            )
            duration_ms = int((time.monotonic() - start_time) * 1000)

            error = result.get("error")
            image_base64 = result.get("image_base64")

            if audit_sink:
                await safe_call(
                    audit_sink,
                    "record_exec",
                    organization_id=organization_id,
                    work_item_id=work_item_id,
                    session_id=str(session_id_uuid) if session_id_uuid else session_id,
                    agent_name=agent_name,
                    command=f"browser_screenshot(full_page={full_page})",
                    exit_code=1 if error else 0,
                    duration_ms=duration_ms,
                )

            if error or not image_base64:
                return f"Error: {error or 'No screenshot data returned'}"

            return ToolImageResult(
                base64_data=image_base64,
                mime_type="image/png",
                file_path="<browser-screenshot>",
            )

        except Exception as e:
            logger.error(f"[browser_screenshot] Error: {e}", exc_info=True)
            return f"Error capturing screenshot: {str(e)}"

    return ToolSpec(
        name="browser_screenshot",
        description=BROWSER_SCREENSHOT_DESCRIPTION,
        parameters=BROWSER_SCREENSHOT_PARAMETERS,
        handler=browser_screenshot,
        returns_multimodal=True,
    )


def create_browser_close_tool_spec(
    session_id: Optional[str] = None,
    sandbox_manager: Optional["SandboxManager"] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for browser_close."""
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def browser_close() -> str:
        logger.info("[browser_close] Closing browser")
        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            result = await resolved_manager.browser_close(session_id=resolved_session_id)
            return f"Browser {result.get('status', 'closed')}"
        except Exception as e:
            logger.error(f"[browser_close] Error: {e}", exc_info=True)
            return f"Error closing browser: {str(e)}"

    return ToolSpec(
        name="browser_close",
        description=BROWSER_CLOSE_DESCRIPTION,
        parameters=BROWSER_CLOSE_PARAMETERS,
        handler=browser_close,
    )


def create_browser_tool_specs(
    session_id: Optional[str] = None,
    sandbox_manager: Optional["SandboxManager"] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
) -> list[ToolSpec]:
    """Convenience bundle: all four browser_* ToolSpecs in the order the
    design doc introduces them (navigate, exec, screenshot, close)."""
    return [
        create_browser_navigate_tool_spec(
            session_id=session_id,
            sandbox_manager=sandbox_manager,
            lazy_runtime=lazy_runtime,
            organization_id=organization_id,
            work_item_id=work_item_id,
            agent_name=agent_name,
            audit_sink=audit_sink,
        ),
        create_browser_exec_tool_spec(
            session_id=session_id,
            sandbox_manager=sandbox_manager,
            lazy_runtime=lazy_runtime,
            organization_id=organization_id,
            work_item_id=work_item_id,
            agent_name=agent_name,
            audit_sink=audit_sink,
        ),
        create_browser_screenshot_tool_spec(
            session_id=session_id,
            sandbox_manager=sandbox_manager,
            lazy_runtime=lazy_runtime,
            organization_id=organization_id,
            work_item_id=work_item_id,
            agent_name=agent_name,
            audit_sink=audit_sink,
        ),
        create_browser_close_tool_spec(
            session_id=session_id,
            sandbox_manager=sandbox_manager,
            lazy_runtime=lazy_runtime,
        ),
    ]
