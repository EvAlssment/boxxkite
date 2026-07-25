"""MCP server exposing a hosted boxkite control-plane's sandbox lifecycle,
exec, and file tools as native MCP tools.

Unlike ``boxkite_client.langchain_tools.create_sandbox_tools`` (which binds
one pre-created ``session_id`` at factory-creation time), every per-sandbox
tool here takes ``session_id`` as a parameter -- the calling MCP client
(Claude Code, Claude Desktop, Codex, Cursor, ...) owns the full lifecycle:
create a sandbox, run several things in it, destroy it, all within one
conversation.

Configuration is read from the environment at import time (``BOXKITE_BASE_URL``,
``BOXKITE_API_KEY``) and the process fails fast with a clear error if either
is missing -- there is no sane default to silently fall back to.

Every tool catches ``BoxkiteApiError``/``BoxkiteConnectionError`` and returns
a descriptive string instead of raising, mirroring the defensive pattern in
``boxkite_client.langchain_tools``: an uncaught exception here would otherwise
propagate up through FastMCP's tool dispatch and come back to the client as a
generic ``isError: true`` result with none of the control-plane's actual
error code/message -- catching locally keeps the message useful.
"""

from __future__ import annotations

import os
import sys
from importlib.metadata import PackageNotFoundError, version as _dist_version
from urllib.parse import urlparse

from boxkite_client import BoxkiteApiError, BoxkiteClient, BoxkiteConnectionError
from mcp.server.fastmcp import FastMCP


class ConfigurationError(RuntimeError):
    """Raised when required boxkite MCP server configuration is missing."""


_LOCALHOST_HOSTNAMES = {"localhost", "127.0.0.1", "::1"}

# LSP CompletionItemKind enum (numeric codes the protocol actually sends,
# same mapping as src/boxkite/tools/lsp_tools.py's _COMPLETION_ITEM_KIND_NAMES
# -- ported rather than imported since this package doesn't depend on the
# root boxkite package, only boxkite_client). Unknown codes fall back to
# "unknown" rather than raising: LSP responses are permissive by spec.
_COMPLETION_ITEM_KIND_NAMES = {
    1: "text", 2: "method", 3: "function", 4: "constructor", 5: "field",
    6: "variable", 7: "class", 8: "interface", 9: "module", 10: "property",
    11: "unit", 12: "value", 13: "enum", 14: "keyword", 15: "snippet",
    16: "color", 17: "file", 18: "reference", 19: "folder", 20: "enum_member",
    21: "constant", 22: "struct", 23: "event", 24: "operator", 25: "type_parameter",
}


def _load_config() -> tuple[str, str]:
    base_url = os.environ.get("BOXKITE_BASE_URL")
    api_key = os.environ.get("BOXKITE_API_KEY")
    missing = [
        name
        for name, value in (("BOXKITE_BASE_URL", base_url), ("BOXKITE_API_KEY", api_key))
        if not value
    ]
    if missing:
        raise ConfigurationError(
            "Missing required environment variable(s): "
            + ", ".join(missing)
            + ". Set BOXKITE_BASE_URL (your control-plane's base URL) and "
            "BOXKITE_API_KEY (a bxk_live_... key from `boxkite auth` or the "
            "hosted dashboard) before starting boxkite-mcp."
        )
    assert base_url is not None and api_key is not None  # narrowed by the check above

    # BoxkiteClient itself also validates this, but checking here first
    # gives a clearer, mcp-specific error message pointing at the env var
    # to fix, rather than a generic ValueError from deep inside the SDK.
    parsed = urlparse(base_url)
    is_https = parsed.scheme == "https"
    is_local_http = parsed.scheme == "http" and parsed.hostname in _LOCALHOST_HOSTNAMES
    if not (is_https or is_local_http):
        raise ConfigurationError(
            f"BOXKITE_BASE_URL={base_url!r} must use https:// (or http://localhost "
            "for local dev only) -- BOXKITE_API_KEY is a full-privilege, long-lived "
            "credential and would otherwise be sent in cleartext."
        )

    return base_url, api_key


def _package_version() -> str:
    """boxkite-mcp's own package version, so the MCP handshake's
    serverInfo.version reports it rather than defaulting to the underlying
    `mcp` library's version. Falls back to "0" only if the distribution
    metadata isn't installed (e.g. an editable checkout without an install)."""
    try:
        return _dist_version("boxkite-mcp")
    except PackageNotFoundError:
        return "0"


def _describe_api_error(action: str, exc: BoxkiteApiError) -> str:
    return f"Error {action}: {exc.message} [{exc.code}] (HTTP {exc.status_code})"


def _describe_connection_error(action: str, exc: BoxkiteConnectionError) -> str:
    return f"Error {action}: could not reach the boxkite control-plane ({exc})"


def build_server(client: BoxkiteClient) -> FastMCP:
    """Construct the FastMCP server and register every sandbox tool against
    ``client``. Split out from ``main()`` so tests can build a server around
    a client backed by ``httpx.MockTransport`` without touching the network
    or process environment.
    """

    mcp = FastMCP(
        name="boxkite",
        instructions=(
            "Tools for creating, using, and destroying boxkite sandboxes -- "
            "isolated, Kubernetes-backed environments for running shell "
            "commands and editing files. Call create_sandbox first to get a "
            "session_id, pass that session_id to exec/file_create/view/"
            "str_replace/ls/glob/grep, and call "
            "destroy_sandbox when done. Use create_sandbox_image/"
            "get_sandbox_image/list_sandbox_images/delete_sandbox_image to "
            "build a custom sandbox image with extra packages baked in, "
            "then pass its id as create_sandbox's image_id. Use "
            "create_sandbox_volume/get_sandbox_volume/list_sandbox_volumes/"
            "delete_sandbox_volume to create independent, persistent "
            "storage volumes, then pass a {volume_id: mount_path} mapping "
            "as create_sandbox's volume_mounts to mount them into a "
            "sandbox. Use create_mcp_connection/list_mcp_connections/"
            "delete_mcp_connection to grant a sandbox network egress to a "
            "curated outbound-MCP catalog entry, then pass its label in "
            "create_sandbox's mcp_connection_names."
        ),
    )
    # FastMCP's constructor takes no `version`, so the low-level server would
    # otherwise report the `mcp` library's version in the MCP handshake's
    # serverInfo. Pin it to this package's own version instead.
    mcp._mcp_server.version = _package_version()

    @mcp.tool()
    def create_sandbox(
        label: str | None = None,
        size: str | None = None,
        storage_gb: float | None = None,
        lifetime_minutes: int | None = None,
        count: int | None = None,
        image_id: str | None = None,
        mcp_connection_names: list[str] | None = None,
        volume_mounts: dict[str, str] | None = None,
        gpu_count: int | None = None,
    ) -> str:
        """Create a new boxkite sandbox and return its session id and status.
        Call this before any other sandbox tool -- every other tool needs the
        session_id this returns.

        size: sandbox size, one of "small", "medium", or "large" -- controls
            the CPU and memory allocated to the sandbox. Defaults to the
            control-plane's default size when omitted.
        storage_gb: size of the sandbox's persistent workspace volume, in
            gigabytes. Defaults to the control-plane's default when omitted.
        lifetime_minutes: how many minutes the sandbox may run before it is
            automatically destroyed. Defaults to the control-plane's default
            lifetime when omitted.
        count: how many identical sandboxes to create in this call. Defaults
            to a single sandbox when omitted.
        image_id: id of a completed custom image built via
            create_sandbox_image -- starts the sandbox from that digest-
            pinned image instead of the operator's default.
        mcp_connection_names: labels of this account's outbound-MCP
            connections (see create_mcp_connection) this session should be
            granted network egress to. A name that doesn't exist for this
            account 404s before any sandbox is created. This only widens the
            session's network egress allowlist to the connection's catalog
            hostname -- there is no MCP-proxy transport yet, so this does
            not yet let the sandbox actually speak MCP protocol to the
            destination.
        volume_mounts: optional {volume_id: mount_path} mapping of
            independent storage volumes created via create_sandbox_volume to
            mount into this sandbox. Every volume_id must already exist for
            this account and be status "ready" -- 404s otherwise.
            mount_path must be an absolute path outside the sandbox's typed
            roots (/workspace, /mnt/*, /tmp).
        gpu_count: opt-in, experimental -- requests this many GPUs for this
            sandbox. Fails with a clear error unless the control-plane
            deployment has GPU support enabled and a GPU-equipped node pool
            provisioned; not verified against real GPU hardware.
        """
        try:
            result = client.create_sandbox(
                label=label,
                size=size,
                storage_gb=storage_gb,
                lifetime_minutes=lifetime_minutes,
                count=count,
                image_id=image_id,
                mcp_connection_names=mcp_connection_names,
                volume_mounts=volume_mounts,
                gpu_count=gpu_count,
            )
        except BoxkiteApiError as exc:
            return _describe_api_error("creating sandbox", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error("creating sandbox", exc)
        return f"Created sandbox {result['id']} (status: {result.get('status', 'unknown')})"

    @mcp.tool()
    def destroy_sandbox(session_id: str) -> str:
        """Tear down a boxkite sandbox by session id. Always call this when
        you're done with a sandbox to free the resource."""
        try:
            client.destroy_sandbox(session_id)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"destroying sandbox {session_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"destroying sandbox {session_id}", exc)
        return f"Destroyed sandbox {session_id}"

    @mcp.tool()
    def get_sandbox(session_id: str) -> str:
        """Look up a single boxkite sandbox's current status by session id."""
        try:
            result = client.get_sandbox(session_id)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"getting sandbox {session_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"getting sandbox {session_id}", exc)
        return f"{result['id']} (status: {result.get('status', 'unknown')}, label: {result.get('label') or '(none)'})"

    @mcp.tool()
    def list_sandboxes(active_only: bool = False) -> str:
        """List sandboxes on this account. Set active_only=true to only see
        sandboxes that are still running."""
        try:
            result = client.list_sandboxes(active_only=active_only)
        except BoxkiteApiError as exc:
            return _describe_api_error("listing sandboxes", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error("listing sandboxes", exc)
        if not result:
            return "No sandboxes found."
        lines = [
            f"- {sb['id']} (status: {sb.get('status', 'unknown')}, label: {sb.get('label') or '(none)'})"
            for sb in result
        ]
        return "\n".join(lines)

    @mcp.tool()
    def create_sandbox_image(
        label: str | None = None,
        base: str = "boxkite-default",
        python_packages: list[str] | None = None,
        apt_packages: list[str] | None = None,
        npm_packages: list[str] | None = None,
    ) -> str:
        """Start building a custom sandbox image with extra packages baked
        in. Returns the new image's id and status -- poll
        get_sandbox_image(id) until status is "completed", then pass
        image_id=id to create_sandbox to start a sandbox from it.

        base: base image to build from, one of "boxkite-default" (the
            operator's standard sandbox image), "boxkite-minimal" (a smaller
            base with fewer preinstalled tools), "boxkite-node" (drops
            Python entirely, for pure JS/TS workloads -- only
            apt_packages/npm_packages are installable), "boxkite-go" (drops
            both Python and Node entirely, for pure Go workloads -- only
            apt_packages are installable), "boxkite-nextjs" (same
            Node-only runtime as "boxkite-node", plus a pre-installed
            Next.js App Router starter vendored at /opt/nextjs-template --
            only apt_packages/npm_packages are installable), or "boxkite-rust"
            (also drops both Python and Node entirely, for pure Rust
            workloads -- only apt_packages are installable). Defaults to
            "boxkite-default".
        python_packages: packages to pip-install into the image. Each entry
            must be exact-version-pinned ("name==version") -- version ranges
            or bare names are rejected with a 422. Not supported on
            base="boxkite-node", base="boxkite-nextjs", base="boxkite-go", or
            base="boxkite-rust".
        apt_packages: packages to apt-install into the image. Same
            exact-version-pinning rule as python_packages.
        npm_packages: packages to npm-install into the image. Same
            exact-version-pinning rule as python_packages. Not supported on
            base="boxkite-go" or base="boxkite-rust".
        """
        try:
            result = client.create_image(
                label=label,
                base=base,
                python_packages=python_packages,
                apt_packages=apt_packages,
                npm_packages=npm_packages,
            )
        except BoxkiteApiError as exc:
            return _describe_api_error("creating sandbox image", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error("creating sandbox image", exc)
        return (
            f"Started building image {result['id']} (status: {result.get('status', 'unknown')}). "
            "Poll get_sandbox_image with this id until status is \"completed\", then pass "
            "image_id to create_sandbox."
        )

    @mcp.tool()
    def get_sandbox_image(image_id: str) -> str:
        """Look up a single custom sandbox image's current build status by
        image id."""
        try:
            result = client.get_image(image_id)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"getting sandbox image {image_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"getting sandbox image {image_id}", exc)
        summary = f"{result['id']} (status: {result.get('status', 'unknown')}, label: {result.get('label') or '(none)'})"
        if result.get("digest"):
            summary += f", digest: {result['digest']}"
        if result.get("failure_reason"):
            summary += f", failure_reason: {result['failure_reason']}"
        return summary

    @mcp.tool()
    def list_sandbox_images() -> str:
        """List custom sandbox images built on this account."""
        try:
            result = client.list_images()
        except BoxkiteApiError as exc:
            return _describe_api_error("listing sandbox images", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error("listing sandbox images", exc)
        if not result:
            return "No images found."
        lines = [
            f"- {image['id']} (status: {image.get('status', 'unknown')}, label: {image.get('label') or '(none)'})"
            for image in result
        ]
        return "\n".join(lines)

    @mcp.tool()
    def delete_sandbox_image(image_id: str) -> str:
        """Delete a custom sandbox image's control-plane record by image id.
        This only removes the bookkeeping row for the image -- any sandboxes
        already running from that image's digest keep running unaffected."""
        try:
            client.delete_image(image_id)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"deleting sandbox image {image_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"deleting sandbox image {image_id}", exc)
        return f"Deleted sandbox image {image_id}"

    @mcp.tool()
    def create_sandbox_volume(label: str | None = None, size_gb: float = 1.0) -> str:
        """Create an independent, persistent storage volume that can later
        be mounted into one or more sandboxes. Returns the new volume's id
        and status -- poll get_sandbox_volume(id) until status is "ready",
        then pass {volume_id: mount_path} as create_sandbox's volume_mounts
        to mount it into a sandbox.

        label: optional human-readable label for the volume.
        size_gb: requested volume size in gigabytes (max 1024). Defaults to
            1.0.
        """
        try:
            result = client.create_volume(label=label, size_gb=size_gb)
        except BoxkiteApiError as exc:
            return _describe_api_error("creating sandbox volume", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error("creating sandbox volume", exc)
        return (
            f"Started creating volume {result['id']} (status: {result.get('status', 'unknown')}). "
            "Poll get_sandbox_volume with this id until status is \"ready\", then pass its id "
            "in create_sandbox's volume_mounts."
        )

    @mcp.tool()
    def get_sandbox_volume(volume_id: str) -> str:
        """Look up a single independent storage volume's current status by
        volume id."""
        try:
            result = client.get_volume(volume_id)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"getting sandbox volume {volume_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"getting sandbox volume {volume_id}", exc)
        summary = f"{result['id']} (status: {result.get('status', 'unknown')}, label: {result.get('label') or '(none)'})"
        if result.get("failure_reason"):
            summary += f", failure_reason: {result['failure_reason']}"
        return summary

    @mcp.tool()
    def list_sandbox_volumes() -> str:
        """List independent storage volumes on this account."""
        try:
            result = client.list_volumes()
        except BoxkiteApiError as exc:
            return _describe_api_error("listing sandbox volumes", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error("listing sandbox volumes", exc)
        if not result:
            return "No volumes found."
        lines = [
            f"- {volume['id']} (status: {volume.get('status', 'unknown')}, label: {volume.get('label') or '(none)'})"
            for volume in result
        ]
        return "\n".join(lines)

    @mcp.tool()
    def delete_sandbox_volume(volume_id: str) -> str:
        """Delete an independent storage volume's control-plane record and
        underlying storage by volume id. Does not retroactively unmount it
        from any already-running sandbox session."""
        try:
            client.delete_volume(volume_id)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"deleting sandbox volume {volume_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"deleting sandbox volume {volume_id}", exc)
        return f"Deleted sandbox volume {volume_id}"

    @mcp.tool()
    def create_mcp_connection(label: str, catalog_id: str) -> str:
        """Grant this account access to one curated outbound-MCP catalog
        entry. Returns the new connection's id and resolved catalog
        hostname -- pass its label in create_sandbox's mcp_connection_names
        to grant a session network egress to it.

        label: unique (per-account) name for this connection.
        catalog_id: which curated MCP catalog entry to grant -- one of
            "slack", "notion", "linear", "github". Restricted to boxkite's
            own reviewed allowlist; never a caller-supplied hostname.

        Note: this only widens a granted session's network egress allowlist
        to the connection's catalog hostname -- there is no MCP-proxy
        transport yet, so this does not yet let a sandbox speak MCP
        protocol to the destination.
        """
        try:
            result = client.create_mcp_connection(label=label, catalog_id=catalog_id)
        except BoxkiteApiError as exc:
            return _describe_api_error("creating MCP connection", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error("creating MCP connection", exc)
        return f"Created MCP connection {result['id']} (label: {result.get('label')}, host: {result.get('host')})"

    @mcp.tool()
    def list_mcp_connections() -> str:
        """List outbound-MCP connections granted on this account."""
        try:
            result = client.list_mcp_connections()
        except BoxkiteApiError as exc:
            return _describe_api_error("listing MCP connections", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error("listing MCP connections", exc)
        if not result:
            return "No MCP connections found."
        lines = [
            f"- {conn['id']} (label: {conn.get('label')}, catalog_id: {conn.get('catalog_id')}, "
            f"host: {conn.get('host')})"
            for conn in result
        ]
        return "\n".join(lines)

    @mcp.tool()
    def delete_mcp_connection(connection_id: str) -> str:
        """Delete an outbound-MCP connection grant by id. Does not affect
        any sandbox already running with this connection's egress already
        granted."""
        try:
            client.delete_mcp_connection(connection_id)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"deleting MCP connection {connection_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"deleting MCP connection {connection_id}", exc)
        return f"Deleted MCP connection {connection_id}"

    @mcp.tool()
    def exec(session_id: str, command: str, timeout: int | None = None) -> str:
        """Run a shell command in a sandbox. Returns stdout, or stderr and
        the exit code if the command failed."""
        try:
            result = client.exec(session_id, command, timeout=timeout)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"running command in sandbox {session_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"running command in sandbox {session_id}", exc)
        if result["exit_code"] != 0:
            return (
                f"Command exited {result['exit_code']}. stdout:\n{result['stdout']}\n"
                f"stderr:\n{result['stderr']}"
            )
        return result["stdout"]

    @mcp.tool()
    def lsp_start(session_id: str, language: str) -> str:
        """Start a persistent language server (pyright for "python",
        typescript-language-server for "typescript"/"javascript") in a
        sandbox. Returns an lsp_id handle to pass to lsp_open/
        lsp_completion/lsp_stop."""
        try:
            result = client.lsp_start(session_id, language)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"starting language server in sandbox {session_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"starting language server in sandbox {session_id}", exc)
        return f"Started language server {result['lsp_id']} for {language}"

    @mcp.tool()
    def lsp_open(session_id: str, lsp_id: str, path: str, content: str) -> str:
        """Open (or, on a later call for the same path, full-document-
        replace) a document on a running language server started by
        lsp_start."""
        try:
            client.lsp_open(session_id, lsp_id, path, content)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"opening {path} on language server {lsp_id} in sandbox {session_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(
                f"opening {path} on language server {lsp_id} in sandbox {session_id}", exc
            )
        return f"Opened {path} on language server {lsp_id}"

    @mcp.tool()
    def lsp_completion(session_id: str, lsp_id: str, path: str, line: int, character: int) -> str:
        """Request completions at a 0-indexed (line, character) position
        from a running language server. `path` must already be open on
        this handle (see lsp_open)."""
        try:
            result = client.lsp_completion(session_id, lsp_id, path, line, character)
        except BoxkiteApiError as exc:
            return _describe_api_error(
                f"requesting completions for {path} on language server {lsp_id} in sandbox {session_id}", exc
            )
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(
                f"requesting completions for {path} on language server {lsp_id} in sandbox {session_id}", exc
            )
        items = result.get("items") or []
        if not items:
            return "No completions found."
        lines = []
        for item in items:
            label = item.get("label", "?")
            kind = item.get("kind")
            kind_name = _COMPLETION_ITEM_KIND_NAMES.get(kind, "unknown") if kind is not None else None
            lines.append(f"- {label}" + (f" ({kind_name})" if kind_name else ""))
        return "\n".join(lines)

    @mcp.tool()
    def lsp_stop(session_id: str, lsp_id: str) -> str:
        """Gracefully shut down a running language server started by
        lsp_start."""
        try:
            client.lsp_stop(session_id, lsp_id)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"stopping language server {lsp_id} in sandbox {session_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"stopping language server {lsp_id} in sandbox {session_id}", exc)
        return f"Stopped language server {lsp_id}"

    @mcp.tool()
    def file_create(session_id: str, path: str, content: str) -> str:
        """Create or overwrite a file in a sandbox's workspace."""
        try:
            result = client.file_create(session_id, path, content)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"creating file {path} in sandbox {session_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"creating file {path} in sandbox {session_id}", exc)
        return f"Wrote {result.get('path', path)} ({result.get('size', len(content))} bytes)"

    @mcp.tool()
    def view(session_id: str, path: str, view_range: list[int] | None = None) -> str:
        """View a file's contents (optionally a line range via view_range
        [start, end]), or list a directory's entries, in a sandbox."""
        try:
            result = client.view(session_id, path, view_range=view_range)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"viewing {path} in sandbox {session_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"viewing {path} in sandbox {session_id}", exc)
        return result["content"] if "content" in result else str(result)

    @mcp.tool()
    def str_replace(
        session_id: str,
        path: str,
        old_str: str,
        new_str: str,
        replace_all: bool = False,
    ) -> str:
        """Replace a string in a sandbox file. By default old_str must appear
        exactly once; set replace_all=true to replace every occurrence."""
        try:
            result = client.str_replace(session_id, path, old_str, new_str, replace_all=replace_all)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"editing {path} in sandbox {session_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"editing {path} in sandbox {session_id}", exc)
        return f"Replaced in {result.get('path', path)} ({result.get('occurrences', 1)} replacement(s))"

    @mcp.tool()
    def ls(session_id: str, path: str = "/") -> str:
        """List the direct children of a directory in a sandbox's workspace.
        Use this before `view` on a directory you haven't explored yet, or
        instead of `exec(session_id, "ls ...")` -- same result, no shell
        round trip."""
        try:
            result = client.ls(session_id, path=path)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"listing {path} in sandbox {session_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"listing {path} in sandbox {session_id}", exc)
        entries = result.get("entries", [])
        if not entries:
            return f"No entries in {path}."
        return "\n".join(str(entry) for entry in entries)

    @mcp.tool()
    def glob(session_id: str, pattern: str, path: str = "/") -> str:
        """Find files by name pattern (e.g. '**/*.py') under a sandbox's
        workspace, starting from path (defaults to the workspace root)."""
        try:
            result = client.glob(session_id, pattern, path=path)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"globbing {pattern!r} in sandbox {session_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"globbing {pattern!r} in sandbox {session_id}", exc)
        matches = result.get("matches", [])
        if not matches:
            return f"No files match {pattern!r} under {path}."
        return "\n".join(str(match) for match in matches)

    @mcp.tool()
    def grep(
        session_id: str,
        pattern: str,
        path: str = "/",
        glob: str | None = None,
        max_matches: int = 500,
    ) -> str:
        """Search file contents by regex pattern under a sandbox's workspace.
        Optionally restrict the search to files matching glob (e.g.
        '*.py'), and cap the number of matches returned with max_matches."""
        try:
            result = client.grep(session_id, pattern, path=path, glob=glob, max_matches=max_matches)
        except BoxkiteApiError as exc:
            return _describe_api_error(f"grepping {pattern!r} in sandbox {session_id}", exc)
        except BoxkiteConnectionError as exc:
            return _describe_connection_error(f"grepping {pattern!r} in sandbox {session_id}", exc)
        if result.get("error"):
            return f"Error grepping {pattern!r} in sandbox {session_id}: {result['error']}"
        matches = result.get("matches", [])
        if not matches:
            return f"No matches for {pattern!r} under {path}."
        lines = [str(match) for match in matches]
        if result.get("truncated"):
            lines.append("(results truncated -- narrow the pattern or path to see more)")
        return "\n".join(lines)

    return mcp


def main() -> None:
    """Entry point for the `boxkite-mcp` console script -- reads config from
    the environment, fails fast if it's missing, then serves over stdio."""
    try:
        base_url, api_key = _load_config()
    except ConfigurationError as exc:
        print(f"boxkite-mcp: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc

    client = BoxkiteClient(base_url=base_url, api_key=api_key)
    server = build_server(client)
    try:
        server.run(transport="stdio")
    finally:
        client.close()


if __name__ == "__main__":
    main()
