"""HTTP Request Tool - secrets-broker HTTP requests via the sidecar
(docs/SECRETS-DESIGN.md).

This is the ONE agent-facing tool that can use a granted secret
(SandboxCreateRequest.secret_names) at all -- an agent references a secret
by name via a literal `{{secret:name}}` token in `headers`/`body`, and the
sidecar itself (never this process, never the sandboxed process) resolves
and substitutes the real value before making the real outbound request. See
sidecar/main.py's `/http-request` route for the substitution, DNS-
rebinding-safe allowlist enforcement, and exact-value response scrubbing
this tool depends on.

This module is framework-agnostic: `create_http_request_tool_spec()`
returns a plain `ToolSpec` (see ./types.py) whose handler is a normal async
callable with no LangChain import anywhere in this file, following the same
shape as bash_tool.py/git_tools.py. `create_http_request_tool()` is a
backward-compatible wrapper that adapts that spec into a LangChain tool (see
./adapters.py) for existing callers. The audit record NEVER includes
header/body values (which may contain `{{secret:name}}` references or,
after substitution server-side, nothing this process ever sees in
plaintext) -- only method/url/status, mirroring sidecar/main.py's own audit
posture for this route (secret_ref + destination host + status code, never
the resolved value).
"""

import logging
from typing import Optional, TYPE_CHECKING
from uuid import UUID

from ..audit import AuditSink, safe_call
from ..lazy_runtime import resolve_sandbox_operation_context
from .types import ToolSpec

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

logger = logging.getLogger(__name__)

_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE"}

HTTP_REQUEST_TOOL_DESCRIPTION = """Make an HTTP request to a third-party API, optionally using a
granted secret's real credential value via a `{{secret:name}}`
reference in `headers`/`body` -- the actual credential is never
visible to you or to any code running in the sandbox; it is
substituted server-side by the sidecar immediately before the
request is sent.

Use `{{secret:<name>}}` (matching a name from this session's
granted secrets) anywhere in a header value or the body, e.g.:
    headers={"Authorization": "Bearer {{secret:prod-stripe}}"}

The destination host must be on that secret's own allowed_hosts
list (configured when the secret was created) -- a mismatched host
is refused with an error, never silently sent to the wrong secret's
allowlist.

Args:
    method: HTTP method (GET, POST, PUT, PATCH, DELETE)
    url: Destination URL (must be absolute, http/https)
    headers: Optional request headers; values may contain
        {{secret:name}} references
    body: Optional request body (string); may contain
        {{secret:name}} references
    timeout: Request timeout in seconds (default 15, max 60)

Returns:
    A summary of the response: status code, headers, and body.
    Any secret value used in this call is scrubbed from the
    response before it reaches you, even if the destination echoed
    it back.
"""

HTTP_REQUEST_TOOL_PARAMETERS = {
    "type": "object",
    "properties": {
        "method": {
            "type": "string",
            "description": "HTTP method (GET, POST, PUT, PATCH, DELETE)",
        },
        "url": {
            "type": "string",
            "description": "Destination URL (must be absolute, http/https)",
        },
        "headers": {
            "type": "object",
            "description": "Optional request headers; values may contain {{secret:name}} references",
        },
        "body": {
            "type": "string",
            "description": "Optional request body (string); may contain {{secret:name}} references",
        },
        "timeout": {
            "type": "integer",
            "description": "Request timeout in seconds (default 15, max 60)",
            "default": 15,
        },
    },
    "required": ["method", "url"],
}


def create_http_request_tool_spec(
    session_id: Optional[str] = None,
    sandbox_manager: Optional['SandboxManager'] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
) -> ToolSpec:
    """
    Build the framework-agnostic ToolSpec for http_request -- the
    secrets-broker HTTP request tool.

    Only usable for a session that was granted secret_names at creation
    time (SandboxCreateRequest.secret_names); a session with no grants can
    still call this tool for a plain (no {{secret:...}} reference) request,
    subject to whatever destination-host restriction the sidecar enforces
    for that call (see sidecar/main.py -- a request referencing no secret
    still goes through the DNS-rebinding-safe check, but has no
    allowed_hosts scoping to enforce since no secret's allowlist applies).

    Returns:
        ToolSpec with a plain async handler(method, url, headers, body, timeout) -> str
    """
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")

    async def http_request(
        method: str,
        url: str,
        headers: Optional[dict] = None,
        body: Optional[str] = None,
        timeout: int = 15,
    ) -> str:
        normalized_method = (method or "GET").strip().upper()
        if normalized_method not in _ALLOWED_METHODS:
            return f"Error: unsupported method {method!r}; must be one of {sorted(_ALLOWED_METHODS)}"

        if not url or not url.strip():
            return "Error: url is required"

        effective_timeout = min(max(1, timeout), 60)

        try:
            resolved_manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=session_id,
            )
            result = await resolved_manager.http_request(
                session_id=resolved_session_id,
                method=normalized_method,
                url=url,
                headers=headers or {},
                body=body,
                timeout=effective_timeout,
            )

            if audit_sink:
                # Deliberately no headers/body here -- see module docstring.
                await safe_call(
                    audit_sink,
                    "record_exec",
                    organization_id=organization_id,
                    work_item_id=work_item_id,
                    session_id=str(session_id) if session_id else resolved_session_id,
                    agent_name=agent_name,
                    command=f"http_request {normalized_method} {url}",
                    exit_code=0 if result.get("status_code", 500) < 400 else 1,
                    duration_ms=0,
                )

            status_code = result.get("status_code")
            response_headers = result.get("headers", {})
            response_body = result.get("body", "")
            truncated_note = " (truncated)" if result.get("truncated") else ""
            return (
                f"Status: {status_code}\n"
                f"Headers: {response_headers}\n"
                f"Body{truncated_note}:\n{response_body}"
            )
        except Exception as e:
            logger.error(f"[http_request] Request error: {e}", exc_info=True)
            return f"Error making HTTP request: {str(e)}"

    return ToolSpec(
        name="http_request",
        description=HTTP_REQUEST_TOOL_DESCRIPTION,
        parameters=HTTP_REQUEST_TOOL_PARAMETERS,
        handler=http_request,
    )


def create_http_request_tool(
    session_id: Optional[str] = None,
    sandbox_manager: Optional['SandboxManager'] = None,
    lazy_runtime: Optional['LazySandboxRuntime'] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
    audit_sink: Optional[AuditSink] = None,
):
    """
    Create the http_request tool as a LangChain tool (backward-compatible
    wrapper).

    Prefer `create_http_request_tool_spec()` for framework-agnostic use --
    this function just adapts that spec via
    boxkite.tools.adapters.to_langchain_tools. Requires the `langchain`
    extra (`pip install boxkite-sandbox[langchain]`).

    Returns:
        LangChain tool
    """
    from .adapters import to_langchain_tools

    spec = create_http_request_tool_spec(
        session_id=session_id,
        sandbox_manager=sandbox_manager,
        lazy_runtime=lazy_runtime,
        organization_id=organization_id,
        work_item_id=work_item_id,
        agent_name=agent_name,
        audit_sink=audit_sink,
    )
    return to_langchain_tools([spec])[0]
