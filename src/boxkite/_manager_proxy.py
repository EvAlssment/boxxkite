"""Sidecar HTTP proxy methods (exec/file/process/interpreter/search/pty) for SandboxManager."""

from ._manager_config import *  # noqa: F401,F403

class SidecarProxyMixin:
    async def execute(
        self,
        session_id: str,
        command: str,
        timeout: int = 30,
        description: Optional[str] = None,
        secret_env: Optional[dict[str, str]] = None,
    ) -> dict:
        """
        Execute bash command in sandbox.

        Args:
            session_id: Session ID
            command: Bash command to execute
            timeout: Command timeout in seconds
            description: Optional description for logging
            secret_env: Optional {env_var_name: granted_secret_name} mapping
                (docs/SECRETS-DESIGN.md's bash_tool addendum) -- the sidecar
                resolves each named secret server-side and injects it into
                the exec'd process's environment; the literal value never
                passes through this manager or appears in `command`.

        Returns:
            Dict with exit_code, stdout, stderr
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/exec", json={
                "command": command,
                "timeout": timeout,
                "description": description,
                "secret_env": secret_env,
            })
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="execute",
            request_fn=_request,
        )

    async def http_request(
        self,
        session_id: str,
        method: str,
        url: str,
        headers: Optional[dict] = None,
        body: Optional[str] = None,
        timeout: int = 15,
    ) -> dict:
        """
        Secrets-broker HTTP request (docs/SECRETS-DESIGN.md §3). Proxies to
        the session's sidecar's `POST /http-request`, which -- not the
        sandboxed process -- builds and sends the real outbound HTTPS
        request, substituting any literal `{{secret:name}}` reference in
        `headers`/`body` for the real value in-process before sending, and
        enforcing a DNS-rebinding-safe destination-host allowlist check
        immediately before connecting. See sidecar/main.py's `/http-request`
        route for the actual substitution/allowlist/scrubbing logic --
        this method is a thin proxy, same shape as `execute()` above.

        Args:
            session_id: Session ID
            method: HTTP method (GET/POST/PUT/PATCH/DELETE)
            url: Destination URL. Its host must be on the allowlist of every
                secret referenced in headers/body.
            headers: Optional request headers; may contain `{{secret:name}}`
                references.
            body: Optional request body (string); may contain
                `{{secret:name}}` references.
            timeout: Request timeout in seconds.

        Returns:
            Dict with status_code, headers, body, truncated -- secret
            values are scrubbed from headers/body before this ever returns.
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/http-request", json={
                "method": method,
                "url": url,
                "headers": headers or {},
                "body": body,
                "timeout": timeout,
            })
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="http_request",
            request_fn=_request,
        )

    async def interpreter_exec(
        self,
        session_id: str,
        code: str,
        timeout: int = 30,
    ) -> dict:
        """
        Execute a code snippet against a persistent, kept-alive Python
        interpreter for the session.

        Unlike execute() (which always runs a fresh `python3 -c ...`
        process via /exec), variables assigned in one call remain visible
        to later calls -- the sidecar keeps one interpreter process alive
        per session until it's reset, idles out, or the session is torn
        down/recycled. See sidecar/main.py's INTERPRETER_* constants.

        Args:
            session_id: Session ID
            code: Python code snippet to execute
            timeout: Call timeout in seconds

        Returns:
            Dict with stdout, result (repr of the last expression, if any),
            error (traceback text, if any), and truncated (bool)
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/interpreter/exec", json={
                "code": code,
                "timeout": timeout,
            })
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="interpreter_exec",
            request_fn=_request,
        )

    async def interpreter_reset(self, session_id: str) -> dict:
        """
        Kill the session's persistent interpreter, if any.

        The next interpreter_exec() call starts a fresh interpreter with an
        empty namespace.

        Args:
            session_id: Session ID

        Returns:
            Dict with status
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/interpreter/reset")
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="interpreter_reset",
            request_fn=_request,
        )

    async def node_interpreter_exec(
        self,
        session_id: str,
        code: str,
        timeout: int = 30,
    ) -> dict:
        """
        Execute a code snippet against a persistent, kept-alive Node.js
        interpreter for the session.

        The Node.js counterpart to interpreter_exec() -- see its docstring
        for the general shape. Requires the sidecar to have
        BOXKITE_NODE_INTERPRETER_ENABLED set; otherwise the sidecar 404s
        (see docs/NODE-INTERPRETER-DESIGN.md).

        Args:
            session_id: Session ID
            code: JavaScript code snippet to execute
            timeout: Call timeout in seconds

        Returns:
            Dict with stdout, result (util.inspect of the last expression's
            value, if any), error (stack text, if any), and truncated (bool)
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/node-interpreter/exec", json={
                "code": code,
                "timeout": timeout,
            })
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="node_interpreter_exec",
            request_fn=_request,
        )

    async def node_interpreter_reset(self, session_id: str) -> dict:
        """
        Kill the session's persistent Node.js interpreter, if any.

        The next node_interpreter_exec() call starts a fresh interpreter
        with empty state.

        Args:
            session_id: Session ID

        Returns:
            Dict with status
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/node-interpreter/reset")
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="node_interpreter_reset",
            request_fn=_request,
        )

    async def lsp_start(self, session_id: str, language: str) -> dict:
        """
        Start a persistent language server for this session
        (docs/LSP-SUPPORT-SCOPING.md, GitHub issue #183). Requires the
        sidecar to have BOXKITE_LSP_ENABLED set; otherwise the sidecar
        404s.

        Args:
            session_id: Session ID
            language: "python" (pyright) or "typescript"
                (typescript-language-server, also covers JavaScript)

        Returns:
            Dict with lsp_id -- an opaque handle to pass to lsp_open/
            lsp_completion/lsp_stop
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/lsp/start", json={"language": language})
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="lsp_start",
            request_fn=_request,
        )

    async def lsp_open(self, session_id: str, lsp_id: str, path: str, content: str) -> dict:
        """
        Open (or, on a later call for the same path, full-document-replace)
        a document on a running language server.

        Args:
            session_id: Session ID
            lsp_id: Handle returned by lsp_start
            path: File path (relative to /workspace, or absolute)
            content: The file's current full content

        Returns:
            Dict with status
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post(
                f"/lsp/{lsp_id}/open", json={"path": path, "content": content}
            )
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="lsp_open",
            request_fn=_request,
        )

    async def lsp_completion(
        self, session_id: str, lsp_id: str, path: str, line: int, character: int
    ) -> dict:
        """
        Request completions at a position from a running language server.

        Args:
            session_id: Session ID
            lsp_id: Handle returned by lsp_start
            path: File path (must already be open on this handle -- see
                lsp_open)
            line: 0-indexed line number
            character: 0-indexed character offset within the line

        Returns:
            Dict with items (raw LSP CompletionItem payloads -- the tool
            layer, not this manager method, translates these into a
            simplified agent-readable shape)
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post(
                f"/lsp/{lsp_id}/completion",
                json={"path": path, "line": line, "character": character},
            )
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="lsp_completion",
            request_fn=_request,
        )

    async def lsp_stop(self, session_id: str, lsp_id: str) -> dict:
        """
        Gracefully shut down a running language server.

        Args:
            session_id: Session ID
            lsp_id: Handle returned by lsp_start

        Returns:
            Dict with status
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post(f"/lsp/{lsp_id}/stop")
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="lsp_stop",
            request_fn=_request,
        )

    async def browser_navigate(
        self,
        session_id: str,
        url: str,
        wait_until: str = "load",
        timeout_seconds: int = 30,
    ) -> dict:
        """
        Load `url` in the session's one headless-Chromium page, lazily
        starting the browser process on first call
        (docs/BROWSER-EXEC-DESIGN.md §2). Requires the sidecar to have
        BOXKITE_BROWSER_ENABLED set; otherwise the sidecar 404s.

        Args:
            session_id: Session ID
            url: URL to navigate to
            wait_until: One of "load", "domcontentloaded", "networkidle",
                "commit"
            timeout_seconds: Call timeout in seconds

        Returns:
            Dict with title, url, status (all None on an application-level
            navigation error), and error (set, non-fatal, on such an error)
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/browser/navigate", json={
                "url": url,
                "wait_until": wait_until,
                "timeout_seconds": timeout_seconds,
            })
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="browser_navigate",
            request_fn=_request,
        )

    async def browser_exec(
        self,
        session_id: str,
        script: str,
        timeout_seconds: int = 10,
    ) -> dict:
        """
        Evaluate `script` in the current page's JS context (Playwright's
        `page.evaluate`, i.e. CDP `Runtime.evaluate`) -- DOM access, basic
        interaction (`el.click()`, ...), reading page state. Lazily starts
        the browser (with a blank page) if it isn't already running.
        Requires BOXKITE_BROWSER_ENABLED; otherwise the sidecar 404s.

        Args:
            session_id: Session ID
            script: JavaScript to evaluate in the page's context
            timeout_seconds: Call timeout in seconds

        Returns:
            Dict with result (the script's JSON-serializable completion
            value) and error (set, non-fatal, if the script threw)
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/browser/exec", json={
                "script": script,
                "timeout_seconds": timeout_seconds,
            })
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="browser_exec",
            request_fn=_request,
        )

    async def browser_screenshot(
        self,
        session_id: str,
        full_page: bool = False,
    ) -> dict:
        """
        Capture the current page as a base64 PNG (Playwright's
        `page.screenshot()` / CDP `Page.captureScreenshot`). Lazily starts
        the browser (with a blank page) if it isn't already running.
        Requires BOXKITE_BROWSER_ENABLED; otherwise the sidecar 404s.

        Args:
            session_id: Session ID
            full_page: Capture the full scrollable page rather than just
                the viewport

        Returns:
            Dict with image_base64 (None on error) and error (set if the
            capture failed, e.g. an oversized payload -- see
            BROWSER_MAX_SCREENSHOT_BYTES)
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/browser/screenshot", json={
                "full_page": full_page,
            })
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="browser_screenshot",
            request_fn=_request,
        )

    async def browser_close(self, session_id: str) -> dict:
        """
        Tear down the session's browser process (idempotent -- a no-op if
        none is running). The next browser_navigate() call starts a fresh
        one. Requires BOXKITE_BROWSER_ENABLED; otherwise the sidecar 404s.

        Args:
            session_id: Session ID

        Returns:
            Dict with status
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/browser/close")
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="browser_close",
            request_fn=_request,
        )

    async def file_create(
        self,
        session_id: str,
        path: str,
        content: str,
        description: Optional[str] = None,
    ) -> dict:
        """
        Create or overwrite file in sandbox.

        Args:
            session_id: Session ID
            path: File path (relative to /workspace)
            content: File content
            description: Optional description

        Returns:
            Dict with path, size, created
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/file-create", json={
                "path": path,
                "content": content,
                "description": description,
            })
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="file_create",
            request_fn=_request,
        )

    async def view(
        self,
        session_id: str,
        path: str,
        view_range: Optional[list[int]] = None,
        description: Optional[str] = None,
    ) -> dict:
        """
        View file contents in sandbox.

        Args:
            session_id: Session ID
            path: File path
            view_range: Optional [start_line, end_line] for partial view
            description: Optional description

        Returns:
            Dict with content, lines, is_directory, entries
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/view", json={
                "path": path,
                "view_range": view_range,
                "description": description,
            })
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="view",
            request_fn=_request,
        )

    async def read_image(
        self,
        session_id: str,
        path: str,
        description: Optional[str] = None,
    ) -> dict:
        """
        Read image bytes from sandbox and return base64 payload + metadata.

        Args:
            session_id: Session ID
            path: Image file path
            description: Optional description

        Returns:
            Dict with path, mime_type, size_bytes, base64_data
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/read-image", json={
                "path": path,
                "description": description,
            })
            if response.status_code == 404:
                detail = None
                try:
                    detail = response.json().get("detail")
                except Exception:
                    detail = response.text
                raise FileNotFoundError(detail or f"Image not found: {path}")
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="read_image",
            request_fn=_request,
        )

    async def str_replace(
        self,
        session_id: str,
        path: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
        description: Optional[str] = None,
    ) -> dict:
        """
        Replace string in file (must appear exactly once).

        Args:
            session_id: Session ID
            path: File path
            old_str: String to replace (must appear exactly once)
            new_str: Replacement string
            description: Optional description

        Returns:
            Dict with path, replaced, occurrences
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/str-replace", json={
                "path": path,
                "old_str": old_str,
                "new_str": new_str,
                "replace_all": replace_all,
                "description": description,
            })
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="str_replace",
            request_fn=_request,
        )

    async def ls(
        self,
        session_id: str,
        path: str = "/",
    ) -> list[dict]:
        """
        List direct children under a directory in sandbox workspace.

        Returns:
            List of file info dicts:
            - path
            - is_dir
            - size
            - modified_at
        """
        async def _request() -> list[dict]:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/ls", json={"path": path})
            response.raise_for_status()
            result = response.json()
            return result.get("entries", [])

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="ls",
            request_fn=_request,
        )

    async def glob(
        self,
        session_id: str,
        pattern: str,
        path: str = "/",
    ) -> list[dict]:
        """
        Find files matching a glob pattern in sandbox workspace.

        Returns:
            List of file info dicts for matching files.
        """
        async def _request() -> list[dict]:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post(
                "/glob",
                json={
                    "pattern": pattern,
                    "path": path,
                },
            )
            response.raise_for_status()
            result = response.json()
            return result.get("matches", [])

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="glob",
            request_fn=_request,
        )

    async def grep(
        self,
        session_id: str,
        pattern: str,
        path: Optional[str] = "/",
        glob: Optional[str] = None,
        max_matches: int = 500,
    ) -> dict:
        """
        Search file contents by regex pattern in sandbox workspace.

        Returns:
            Dict with:
            - matches: list of {path, line, text}
            - error: optional error string
            - truncated: bool
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post(
                "/grep",
                json={
                    "pattern": pattern,
                    "path": path,
                    "glob": glob,
                    "max_matches": max_matches,
                },
            )
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="grep",
            request_fn=_request,
        )

    async def watch_directory(
        self,
        session_id: str,
        path: str = "/",
        timeout_seconds: float = 10.0,
    ) -> dict:
        """
        Long-poll for the first batch of filesystem changes under `path`
        (docs/FILE-WATCHER-DESIGN.md). Blocks server-side up to
        `timeout_seconds` (sidecar-capped at 60s) -- well under this
        client's own REQUEST_TIMEOUT (120s), so the long-poll always has
        room to complete before the HTTP client's own timeout would fire.

        Stateless, single-call semantics: a change that happens in the gap
        between two calls (no watch open at that moment) is not reported --
        see the sidecar's own `/watch` docstring for why that's a
        deliberate first-pass simplification, not an oversight.

        Returns:
            Dict with:
            - changes: list of {path, event} (event is one of "created",
              "modified", "deleted", "moved_from", "moved_to")
            - timed_out: bool -- True if no change was seen within timeout_seconds
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post(
                "/watch",
                json={"path": path, "timeout_seconds": timeout_seconds},
            )
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="watch_directory",
            request_fn=_request,
        )

    async def pty_exec(
        self,
        session_id: str,
        command: str,
        input_bytes: bytes = b"",
        timeout_seconds: float = 30.0,
    ) -> dict:
        """
        Run one command behind a real pseudo-terminal (docs/AGENT-PTY-DESIGN.md,
        option A) -- for curses/readline programs that check `isatty()` and
        don't behave correctly over `bash_tool`'s plain pipe.

        404s unless the sidecar has `BOXKITE_AGENT_PTY_ENABLED=true` set --
        this is new, off-by-default attack surface (see the sidecar route's
        own docstring), not something every deployment gets automatically.

        Args:
            session_id: Session ID
            command: Command to run, split like a shell would (via shlex)
                but NEVER passed through an actual shell -- shell
                metacharacters (`;`, `&&`, `$(...)`) are inert, literal argv.
            input_bytes: Raw bytes to write to the PTY (e.g. an answer to
                an interactive prompt).
            timeout_seconds: How long to wait before killing the process
                (sidecar-capped at 120s).

        Returns:
            Dict with:
            - output: captured PTY output (str)
            - exit_code: int or None if the process was killed on timeout
            - timed_out: bool
        """
        import base64 as _base64

        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post(
                "/pty-exec",
                json={
                    "command": command,
                    "input_bytes": _base64.b64encode(input_bytes).decode() if input_bytes else "",
                    "timeout_seconds": timeout_seconds,
                },
            )
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="pty_exec",
            request_fn=_request,
        )

    # =========================================================================
    # Background process/session operations (proxy to sidecar /process/*)
    #
    # Distinct from `execute()`/`/exec`: these track a process across multiple
    # HTTP calls instead of awaiting it to completion in one request. See
    # docs/PROCESS-SESSIONS-DESIGN.md.
    # =========================================================================

    async def start_process(
        self,
        session_id: str,
        command: str,
        description: Optional[str] = None,
        max_runtime_seconds: int = 3600,
        expose_port: Optional[int] = None,
    ) -> dict:
        """
        Start a background process in the sandbox, tracked by the sidecar
        across multiple HTTP calls until it exits or is stopped.

        `expose_port` (see docs/NETWORK-INGRESS-DESIGN.md) opts this one
        process out of the fresh per-exec network namespace so its
        listening port can be reverse-proxied via a preview URL
        (`create_preview_token`/control-plane's `/preview/{port}` routes).
        Leave unset for a normal, fully network-isolated background process.

        Returns:
            Dict with process_id, status, started_at
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/process/start", json={
                "command": command,
                "description": description,
                "max_runtime_seconds": max_runtime_seconds,
                "expose_port": expose_port,
            })
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="start_process",
            request_fn=_request,
        )

    async def get_process_output(
        self,
        session_id: str,
        process_id: str,
        since_offset: int = 0,
    ) -> dict:
        """
        Poll a background process's output since a given byte offset.

        Returns:
            Dict with status, stdout_chunk, next_offset, truncated, exit_code
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.get(
                f"/process/{process_id}/output",
                params={"since_offset": since_offset},
            )
            if response.status_code == 404:
                detail = None
                try:
                    detail = response.json().get("detail")
                except Exception:
                    detail = response.text
                raise ValueError(detail or f"Process not found: {process_id}")
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="get_process_output",
            request_fn=_request,
        )

    async def send_process_input(
        self,
        session_id: str,
        process_id: str,
        data: str,
    ) -> dict:
        """
        Write to a tracked background process's stdin pipe.

        Returns:
            Dict with bytes_written
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post(
                f"/process/{process_id}/input",
                json={"data": data},
            )
            if response.status_code == 404:
                detail = None
                try:
                    detail = response.json().get("detail")
                except Exception:
                    detail = response.text
                raise ValueError(detail or f"Process not found: {process_id}")
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="send_process_input",
            request_fn=_request,
        )

    async def stop_process(
        self,
        session_id: str,
        process_id: str,
    ) -> dict:
        """
        Stop a tracked background process: SIGTERM, grace period, SIGKILL.

        Returns:
            Dict with status, exit_code
        """
        async def _request() -> dict:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post(f"/process/{process_id}/stop")
            if response.status_code == 404:
                detail = None
                try:
                    detail = response.json().get("detail")
                except Exception:
                    detail = response.text
                raise ValueError(detail or f"Process not found: {process_id}")
            response.raise_for_status()
            return response.json()

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="stop_process",
            request_fn=_request,
        )

    async def list_processes(
        self,
        session_id: str,
    ) -> list[dict]:
        """
        List every background process currently tracked for this session.

        Returns:
            List of dicts with process_id, command, description, status,
            started_at, exit_code
        """
        async def _request() -> list[dict]:
            pod_name, pod_ip = await self._resolve_session(session_id)
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.get("/process")
            response.raise_for_status()
            result = response.json()
            return result.get("processes", [])

        return await self._call_sidecar_with_recovery(
            session_id=session_id,
            operation="list_processes",
            request_fn=_request,
        )

    # =========================================================================
    # Network ingress preview (proxy to sidecar /preview/{port}/...)
    #
    # See docs/NETWORK-INGRESS-DESIGN.md. Deliberately NOT wrapped in
    # `_call_sidecar_with_recovery`: that helper exists to transparently
    # replay a request against a freshly-recovered pod after a transport
    # error, which is safe for the idempotent, side-effect-free calls it's
    # used for elsewhere (exec, file ops). A preview request forwards an
    # arbitrary caller-chosen HTTP method (POST/PUT/DELETE included) to
    # whatever the agent's own dev server does with it -- silently replaying
    # that against a different (recovered) pod would replay a
    # possibly-non-idempotent request without the caller's knowledge. A
    # transport failure here surfaces directly as a 502 to the preview
    # caller instead.
    # =========================================================================

    async def proxy_preview_request(
        self,
        session_id: str,
        port: int,
        path: str,
        method: str,
        params: Optional[dict] = None,
        headers: Optional[dict] = None,
        content: bytes = b"",
    ) -> httpx.Response:
        """
        Forward one HTTP request to the sidecar's `/preview/{port}/{path}`,
        which in turn reverse-proxies it to `127.0.0.1:{port}` inside the
        pod (see sidecar/main.py's `preview_proxy`).

        Returns a STREAMING httpx.Response (built via `client.send(...,
        stream=True)`, mirroring the sidecar's own upstream hop) rather than
        one with the body already read into memory -- see
        docs/NETWORK-INGRESS-DESIGN.md's former "no true streaming"
        limitation, closed by this change. Only the response status/headers
        are awaited here; the body is NOT read yet. The caller (the
        control-plane's preview proxy route) is responsible for draining it
        via `response.aiter_bytes()` and calling `await response.aclose()`
        when done, exactly like the sidecar's own `preview_proxy` does for
        its upstream leg -- this method deliberately does not close the
        response itself, since closing it here before the caller has had a
        chance to stream the body would defeat the entire point.

        Raises the same ValueError/RuntimeError as `_resolve_session` for an
        unknown or unreachable session -- callers translate that into a
        404/502 the same way every other sandbox route does. A transport
        error while sending the request (e.g. the sidecar itself is
        unreachable) raises the underlying httpx exception, which the
        caller translates into a 502.
        """
        pod_name, pod_ip = await self._resolve_session(session_id)
        http_client = self._get_http_client(pod_name, pod_ip)
        normalized_path = path.lstrip("/")
        request = http_client.build_request(
            method,
            f"/preview/{port}/{normalized_path}",
            params=params,
            headers=headers,
            content=content,
        )
        return await http_client.send(request, stream=True)

    async def _kill_all_processes(self, pod_name: str, pod_ip: str) -> None:
        """
        SIGKILL every background process tracked by the pod at `pod_ip`.

        Called from `destroy_session`/`_recycle_pod_via_k8s` *before* the
        existing `/configure` wipe call -- see
        docs/PROCESS-SESSIONS-DESIGN.md sections 2(b)/5. This is the mandatory
        fix for the cross-tenant leak pod recycling would otherwise
        introduce: without it, a background process (and its buffered
        stdout) started by the tenant being torn down could still be
        running, or still be observable via list_processes/
        get_process_output, once the pod is claimed by a different tenant.
        Best-effort: a failure here must not block teardown, since
        `/configure`'s own internal kill-all call (sidecar/main.py) is
        defense in depth for exactly this case.
        """
        try:
            http_client = self._get_http_client(pod_name, pod_ip)
            response = await http_client.post("/process/kill-all")
            response.raise_for_status()
        except Exception as e:
            logger.warning(
                f"[SandboxManager] Failed to kill tracked processes on pod {pod_name} "
                f"before teardown: {e}"
            )

