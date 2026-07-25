"""
Git Tools - clone/status/add/commit/push/pull/branch/checkout in the sandbox

Scoped per docs/GIT-OPERATIONS-DESIGN.md: one tool per operation, shelling
out through the same exec path bash_tool.py already uses (SandboxManager.
execute(), which POSTs to the sidecar's /exec route and runs `sh -c
<command>` inside the sandbox namespace). There is no new sidecar route and
no new execution primitive here -- this module only shapes git-specific
argument construction and credential handling on top of the existing,
already-reviewed exec path.

DEVIATION FROM THE DESIGN DOC (documented, not silent -- see the doc's own
"Before anyone implements this" section, which explicitly gates
implementation on a maintainer review pass and a dedicated network-
isolation-exception spike that has not happened):

- Section 2.4 (network egress) proposes toggling
  SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED per-exec-call, scoped to just the
  git-issued call. That requires new sidecar control flow in
  build_k8s_exec_command and is explicitly flagged in the design doc as
  "unresolved," not a verified mechanism, and it needs its own spike before
  being built. This module does NOT implement a per-call network-isolation
  override. `git_clone`/`git_push`/`git_pull` reach the network exactly as
  far as the *existing*, pod-wide `SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED`
  setting and `deploy/network-policy.yaml` egress allowlist already permit
  -- by default, that is "no network access at all," so these three tools
  will fail with a network-unreachable error unless an operator has
  explicitly configured a session/pod with network egress enabled and a
  git-host allowlist rule (see the example block added to
  deploy/network-policy.yaml). This is the conservative, no-new-attack-
  surface choice explicitly endorsed by the design doc's own "do not make
  egress network isolation session-wide just to support git" language --
  it costs usability (git_clone/push/pull are inert on a default pod) in
  exchange for not inventing a new, unreviewed isolation-bypass mechanism.
- No SandboxCreateRequest/control-plane schema change (the optional
  `git_remote_hosts` field from design doc §3) is included -- it is only
  needed if/when the per-call network exception above is built.

Credential handling (docs/GIT-OPERATIONS-DESIGN.md §2.1):

- Callers pass `token=` (HTTPS PAT) or `ssh_key=` (private key PEM/OpenSSH
  text) as a parameter on the tool call itself -- never at session/tool
  construction time, never as a long-lived env var, never persisted by
  this module or SandboxManager.
- Both are written, for the single call only, to a file under /tmp via
  SandboxManager.file_create() (the sidecar's /file-create route logs only
  the destination *path*, never request content -- see sidecar/main.py's
  `logger.info(f"[file-create] {full_path}")` -- unlike /exec, which logs
  the first 100 chars of the *command text*). The credential value is
  therefore never present in anything logged by this feature.
- The git invocation itself references the credential file through git's
  own file-reading config machinery, never a shell: the HTTPS/token path
  writes a real gitconfig snippet (`[http]\n\textraHeader = ...`) to the
  temp file and passes `-c include.path=<path>` (git parses and includes
  the file itself; no shell is involved in getting the secret from disk
  into git's config), and the SSH path passes `-i <path>`. Earlier code
  in this module built the config value as a literal
  `f'http.extraHeader=...: Basic $(cat {path})'` string and relied on the
  *sidecar's* `sh -c` to expand the `$(cat ...)` -- that never actually
  happened (the whole string, `$(...)` included, was shlex-quoted as one
  argv token before being handed to the shell, so the substitution was
  syntactically inert) and was also just a dangerous pattern to have in a
  trust-sensitive tool even though it wasn't exploitable given how
  `cred_path` is generated. `include.path` closes both problems: no
  shell-metacharacter-shaped string is ever constructed, and the raw
  secret still never appears in the command *string* handed to
  SandboxManager.execute(), so it can't leak through the /exec route's
  command-prefix logging either.
- The temp file is deleted in a `finally` block after exactly one git
  invocation, regardless of success or failure -- never left for a later
  cleanup pass.
- /tmp is one of the sidecar's typed roots (sidecar/main.py's
  `_typed_allowed_roots()`) and is explicitly documented, in both
  bash_tool.py and file_tools.py, as NOT synced by `_periodic_sync_loop()`
  and NOT exposed via present_files/view/ls's default listings -- this is
  the load-bearing property the design doc's §5 calls out as needing a
  dedicated test (see tests/test_git_tools.py).

This module is framework-agnostic: each `create_git_*_tool_spec()` function
returns a plain `ToolSpec` (see ./types.py) whose handler is a normal async
callable with no LangChain import anywhere in this file. `create_git_tools()`
and `create_git_*_tool()` are backward-compatible wrappers that adapt those
specs into LangChain tools (see ./adapters.py) for existing callers.
`create_git_tool_specs()` is the framework-agnostic equivalent of
`create_git_tools()`, returning `list[ToolSpec]`.
"""

import base64
import logging
import shlex
from typing import Optional, TYPE_CHECKING
from uuid import UUID, uuid4

from ..audit import AuditSink, safe_call
from ..lazy_runtime import resolve_sandbox_operation_context
from .types import ToolSpec

if TYPE_CHECKING:
    from ..manager import SandboxManager
    from ..lazy_runtime import LazySandboxRuntime

logger = logging.getLogger(__name__)

DEFAULT_WORKSPACE = "/workspace"
CLONE_TIMEOUT_SEC = 300
NETWORK_OP_TIMEOUT_SEC = 300
LOCAL_OP_TIMEOUT_SEC = 60

# git URLs this tool set will act on. Deliberately excludes file:// and
# other local-filesystem schemes -- a git_clone/push/pull call is meant to
# reach a *remote* git host, and accepting file:// would let a caller-
# supplied "url" read arbitrary sandbox-local paths through git's own
# protocol handling instead of the tool's own path parameter validation.
ALLOWED_URL_PREFIXES = ("https://", "http://", "git@", "ssh://")


def _quote(value: str) -> str:
    return shlex.quote(value)


def _canonical_session_id(session_id: Optional[str]) -> Optional[str]:
    return str(session_id).strip() if session_id else None


def _session_id_uuid(canonical_session_id: Optional[str]) -> Optional[UUID]:
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
        return None


def _basic_auth_header_value(token: str) -> str:
    """Build the base64 value for `Authorization: Basic <value>`.

    Uses `x-access-token` as the username, which GitHub/GitLab/Bitbucket
    all accept as a placeholder username paired with a real PAT as the
    password over HTTPS.
    """
    raw = f"x-access-token:{token}".encode("utf-8")
    return base64.b64encode(raw).decode("ascii")


def _http_extra_header_gitconfig(header_value: str) -> str:
    """Render a gitconfig file snippet setting http.extraHeader.

    Written to the /tmp credential file and referenced via
    `-c include.path=<path>` so git reads the real secret from disk
    through its own config parser -- never through a shell command
    substitution, and never as a literal value in the command string
    handed to SandboxManager.execute(). `header_value` is always the
    base64 output of `_basic_auth_header_value()`, so it's guaranteed to
    contain only base64-alphabet characters (no `;`, `#`, quotes, or
    newlines that could otherwise be misparsed as gitconfig syntax).
    """
    return f"[http]\n\textraHeader = Authorization: Basic {header_value}\n"


async def _write_temp_credential(
    manager: "SandboxManager",
    session_id: Optional[str],
    content: str,
) -> str:
    """Write short-lived credential material to a /tmp-only path.

    Returns the sandbox-visible path. Never writes under /workspace,
    /mnt/user-data/outputs, or anywhere else `_periodic_sync_loop()` or
    `present_files`/`view`/`ls` would surface it.
    """
    path = f"/tmp/boxkite-git-cred-{uuid4().hex}"
    await manager.file_create(session_id=session_id, path=path, content=content)
    # SECURITY: restrict to owner-read/write only, matching the design
    # doc's explicit 0600 requirement for any credential material touching
    # the sandbox filesystem.
    chmod_result = await manager.execute(
        session_id=session_id,
        command=f"chmod 600 {_quote(path)}",
        timeout=LOCAL_OP_TIMEOUT_SEC,
    )
    if chmod_result.get("exit_code", 0) != 0:
        # Best effort cleanup before surfacing the failure -- an unreadable
        # perms-hardening failure must not leave the credential behind.
        await _delete_temp_credential(manager, session_id, path)
        raise RuntimeError(
            f"Failed to secure credential file permissions: {chmod_result.get('stderr', '')}"
        )
    return path


async def _delete_temp_credential(
    manager: "SandboxManager",
    session_id: Optional[str],
    path: str,
) -> None:
    try:
        await manager.execute(
            session_id=session_id,
            command=f"rm -f {_quote(path)}",
            timeout=LOCAL_OP_TIMEOUT_SEC,
        )
    except Exception as exc:
        logger.warning(f"[git_tools] Failed to delete temp credential {path}: {exc}")


def _format_result(operation: str, exit_code: int, stdout: str, stderr: str) -> str:
    output = stdout.strip()
    if stderr.strip():
        output = f"{output}\n{stderr.strip()}" if output else stderr.strip()
    if exit_code != 0:
        return f"Error running git {operation} (exit code {exit_code}):\n{output}"
    return output if output else f"git {operation} completed successfully."


async def _run_git(
    manager: "SandboxManager",
    session_id: Optional[str],
    *,
    path: str,
    argv: list[str],
    env_prefix: str = "",
    timeout: int = LOCAL_OP_TIMEOUT_SEC,
) -> dict:
    git_command = " ".join(_quote(part) for part in argv)
    full_command = f"cd {_quote(path)} && {env_prefix}git {git_command}".strip()
    return await manager.execute(
        session_id=session_id,
        command=full_command,
        timeout=timeout,
    )


def _validate_clone_url(url: str) -> Optional[str]:
    if not url or not url.strip():
        return "Error: url is required"
    if not url.startswith(ALLOWED_URL_PREFIXES):
        return (
            "Error: url must use https://, http://, ssh://, or git@ form "
            "(e.g. 'https://github.com/org/repo.git'). Local file:// paths "
            "are not supported by this tool."
        )
    return None


async def _record_audit(
    audit_sink: Optional[AuditSink],
    *,
    organization_id: Optional[UUID],
    work_item_id: Optional[UUID],
    session_id: Optional[str],
    agent_name: Optional[str],
    command_for_audit: str,
    exit_code: int,
) -> None:
    """Mirror a git operation into the optional AuditSink as an exec record.

    `command_for_audit` must already have any credential material stripped
    -- callers pass a reconstructed, credential-free description of the
    operation (e.g. "git push origin main"), never the literal command
    string that may reference a /tmp credential file.
    """
    if not audit_sink:
        return
    await safe_call(
        audit_sink,
        "record_exec",
        organization_id=organization_id,
        work_item_id=work_item_id,
        session_id=session_id,
        agent_name=agent_name,
        command=command_for_audit,
        exit_code=exit_code,
        duration_ms=0,
    )


def _tool_context(
    sandbox_manager: Optional["SandboxManager"],
    lazy_runtime: Optional["LazySandboxRuntime"],
    session_id: Optional[str],
    organization_id: Optional[UUID],
    work_item_id: Optional[UUID],
    agent_name: Optional[str],
):
    if sandbox_manager is None and lazy_runtime is None:
        raise ValueError("sandbox_manager must be provided")
    canonical_session_id = _canonical_session_id(session_id)
    session_uuid = _session_id_uuid(canonical_session_id)
    return canonical_session_id, session_uuid


GIT_CLONE_DESCRIPTION = """
Clone a git repository into the sandbox workspace.

Requires the sandbox to have network egress to the git host --
by default the sandbox has NO network access at all, so this will
fail unless the operator has explicitly configured egress for the
target host (see deploy/network-policy.yaml's git-host example
block and SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED).

Credentials, if provided, are used for exactly this one clone and
are never written anywhere but a /tmp file deleted immediately
after this call completes (success or failure) -- never persisted,
never logged.

SECURITY: clones with `--no-local` and never passes
`--recurse-submodules` -- a malicious repo's submodule URLs are a
known code-execution vector and are not fetched by this tool.

Args:
    url: Repository URL (https://, ssh://, or git@ form)
    path: Destination directory in the sandbox (default /workspace/repo)
    branch: Optional branch to clone (default: remote's default branch)
    depth: Optional shallow-clone depth (e.g. 1 for the latest commit only)
    token: Optional short-lived HTTPS personal access token, used only
        for this call
    ssh_key: Optional short-lived SSH private key text, used only for
        this call (requires an ssh:// or git@ url)

Returns:
    Human-readable summary of the clone, or an error message
"""

GIT_CLONE_PARAMETERS = {
    "type": "object",
    "properties": {
        "url": {
            "type": "string",
            "description": "Repository URL (https://, ssh://, or git@ form)",
        },
        "path": {
            "type": "string",
            "description": "Destination directory in the sandbox (default /workspace/repo)",
            "default": f"{DEFAULT_WORKSPACE}/repo",
        },
        "branch": {
            "type": "string",
            "description": "Optional branch to clone (default: remote's default branch)",
        },
        "depth": {
            "type": "integer",
            "description": "Optional shallow-clone depth (e.g. 1 for the latest commit only)",
        },
        "token": {
            "type": "string",
            "description": "Optional short-lived HTTPS personal access token, used only for this call",
        },
        "ssh_key": {
            "type": "string",
            "description": "Optional short-lived SSH private key text, used only for this call (requires an ssh:// or git@ url)",
        },
    },
    "required": ["url"],
}


def create_git_clone_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    audit_sink: Optional[AuditSink] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for git_clone."""
    canonical_session_id, session_uuid = _tool_context(
        sandbox_manager, lazy_runtime, session_id, organization_id, work_item_id, agent_name
    )

    async def git_clone(
        url: str,
        path: str = f"{DEFAULT_WORKSPACE}/repo",
        branch: Optional[str] = None,
        depth: Optional[int] = None,
        token: Optional[str] = None,
        ssh_key: Optional[str] = None,
    ) -> str:
        url_error = _validate_clone_url(url)
        if url_error:
            return url_error
        if not path or not path.strip():
            return "Error: path is required"
        path = path.strip()

        try:
            manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=canonical_session_id,
            )
        except Exception as e:
            logger.error(f"[git_clone] Failed to resolve sandbox context: {e}", exc_info=True)
            return f"Error resolving sandbox: {str(e)}"

        argv = ["clone", "--no-local"]
        if depth:
            argv += ["--depth", str(int(depth))]
        if branch:
            argv += ["--branch", branch]

        cred_path: Optional[str] = None
        env_prefix = ""
        extra_config: list[str] = []
        try:
            if token:
                header_value = _basic_auth_header_value(token)
                cred_path = await _write_temp_credential(
                    manager, resolved_session_id, _http_extra_header_gitconfig(header_value)
                )
                # git reads the real header value from the /tmp gitconfig
                # file itself via `include.path` -- no shell command
                # substitution, so it never appears in the command string
                # handed to SandboxManager.execute() (and therefore never
                # in the sidecar's truncated /exec log line).
                extra_config = ["-c", f"include.path={cred_path}"]
            elif ssh_key:
                key_content = ssh_key if ssh_key.endswith("\n") else ssh_key + "\n"
                cred_path = await _write_temp_credential(manager, resolved_session_id, key_content)
                env_prefix = (
                    f"GIT_SSH_COMMAND={_quote('ssh -i ' + cred_path + ' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new')} "
                )

            full_argv = extra_config + argv + [url, path]
            result = await _run_git(
                manager,
                resolved_session_id,
                path="/",
                argv=full_argv,
                env_prefix=env_prefix,
                timeout=CLONE_TIMEOUT_SEC,
            )
        except Exception as e:
            logger.error(f"[git_clone] Error: {e}", exc_info=True)
            return f"Error cloning repository: {str(e)}"
        finally:
            if cred_path:
                await _delete_temp_credential(manager, resolved_session_id, cred_path)

        exit_code = result.get("exit_code", 0)
        await _record_audit(
            audit_sink,
            organization_id=organization_id,
            work_item_id=work_item_id,
            session_id=str(session_uuid) if session_uuid else canonical_session_id,
            agent_name=agent_name,
            command_for_audit=f"git clone {url} {path}",
            exit_code=exit_code,
        )
        return _format_result("clone", exit_code, result.get("stdout", ""), result.get("stderr", ""))

    return ToolSpec(
        name="git_clone",
        description=GIT_CLONE_DESCRIPTION,
        parameters=GIT_CLONE_PARAMETERS,
        handler=git_clone,
    )


def create_git_clone_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    audit_sink: Optional[AuditSink] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
):
    """Create the git_clone tool as a LangChain tool (backward-compatible wrapper)."""
    from .adapters import to_langchain_tools

    spec = create_git_clone_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
        audit_sink=audit_sink,
        organization_id=organization_id,
        work_item_id=work_item_id,
        agent_name=agent_name,
    )
    return to_langchain_tools([spec])[0]


GIT_STATUS_DESCRIPTION = """
Show the working tree status of a git repository in the sandbox.

Args:
    path: Path to the repository (default /workspace)

Returns:
    `git status` output, or an error message
"""

GIT_STATUS_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the repository (default /workspace)",
            "default": DEFAULT_WORKSPACE,
        },
    },
    "required": [],
}


def create_git_status_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for git_status (read-only)."""
    canonical_session_id, _ = _tool_context(sandbox_manager, lazy_runtime, session_id, None, None, None)

    async def git_status(path: str = DEFAULT_WORKSPACE) -> str:
        path = (path or DEFAULT_WORKSPACE).strip() or DEFAULT_WORKSPACE
        try:
            manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=canonical_session_id,
            )
            result = await _run_git(
                manager,
                resolved_session_id,
                path=path,
                argv=["status", "--porcelain=v1", "--branch"],
            )
        except Exception as e:
            logger.error(f"[git_status] Error: {e}", exc_info=True)
            return f"Error running git status: {str(e)}"

        return _format_result(
            "status", result.get("exit_code", 0), result.get("stdout", ""), result.get("stderr", "")
        )

    return ToolSpec(
        name="git_status",
        description=GIT_STATUS_DESCRIPTION,
        parameters=GIT_STATUS_PARAMETERS,
        handler=git_status,
    )


def create_git_status_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
):
    """Create the git_status tool as a LangChain tool (backward-compatible wrapper)."""
    from .adapters import to_langchain_tools

    spec = create_git_status_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]


GIT_ADD_DESCRIPTION = """
Stage files for commit in a git repository.

Args:
    path: Path to the repository (default /workspace)
    files: Files/patterns to stage, relative to `path` (default: all
        changes, equivalent to `git add -A`)

Returns:
    Confirmation message, or an error message
"""

GIT_ADD_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the repository (default /workspace)",
            "default": DEFAULT_WORKSPACE,
        },
        "files": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Files/patterns to stage, relative to `path` (default: all changes, equivalent to `git add -A`)",
        },
    },
    "required": [],
}


def create_git_add_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for git_add."""
    canonical_session_id, _ = _tool_context(sandbox_manager, lazy_runtime, session_id, None, None, None)

    async def git_add(path: str = DEFAULT_WORKSPACE, files: Optional[list[str]] = None) -> str:
        path = (path or DEFAULT_WORKSPACE).strip() or DEFAULT_WORKSPACE
        targets = files if files else ["-A"]
        if not isinstance(targets, list) or not all(isinstance(f, str) and f for f in targets):
            return "Error: files must be a non-empty list of strings"

        try:
            manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=canonical_session_id,
            )
            result = await _run_git(
                manager,
                resolved_session_id,
                path=path,
                argv=["add"] + targets,
            )
        except Exception as e:
            logger.error(f"[git_add] Error: {e}", exc_info=True)
            return f"Error staging files: {str(e)}"

        return _format_result(
            "add", result.get("exit_code", 0), result.get("stdout", ""), result.get("stderr", "")
        )

    return ToolSpec(
        name="git_add",
        description=GIT_ADD_DESCRIPTION,
        parameters=GIT_ADD_PARAMETERS,
        handler=git_add,
    )


def create_git_add_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
):
    """Create the git_add tool as a LangChain tool (backward-compatible wrapper)."""
    from .adapters import to_langchain_tools

    spec = create_git_add_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]


GIT_COMMIT_DESCRIPTION = """
Commit staged changes in a git repository.

Args:
    message: Commit message (required)
    path: Path to the repository (default /workspace)
    author_name: Optional commit author name override
    author_email: Optional commit author email override

Returns:
    Confirmation message with the new commit, or an error message
"""

GIT_COMMIT_PARAMETERS = {
    "type": "object",
    "properties": {
        "message": {
            "type": "string",
            "description": "Commit message (required)",
        },
        "path": {
            "type": "string",
            "description": "Path to the repository (default /workspace)",
            "default": DEFAULT_WORKSPACE,
        },
        "author_name": {
            "type": "string",
            "description": "Optional commit author name override",
        },
        "author_email": {
            "type": "string",
            "description": "Optional commit author email override",
        },
    },
    "required": ["message"],
}


def create_git_commit_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    audit_sink: Optional[AuditSink] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for git_commit."""
    canonical_session_id, session_uuid = _tool_context(
        sandbox_manager, lazy_runtime, session_id, organization_id, work_item_id, agent_name
    )

    async def git_commit(
        message: str,
        path: str = DEFAULT_WORKSPACE,
        author_name: Optional[str] = None,
        author_email: Optional[str] = None,
    ) -> str:
        if not message or not message.strip():
            return "Error: message is required"
        path = (path or DEFAULT_WORKSPACE).strip() or DEFAULT_WORKSPACE

        argv = []
        if author_name:
            argv += ["-c", f"user.name={author_name}"]
        if author_email:
            argv += ["-c", f"user.email={author_email}"]
        argv += ["commit", "-m", message]

        try:
            manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=canonical_session_id,
            )
            result = await _run_git(manager, resolved_session_id, path=path, argv=argv)
        except Exception as e:
            logger.error(f"[git_commit] Error: {e}", exc_info=True)
            return f"Error committing changes: {str(e)}"

        exit_code = result.get("exit_code", 0)
        await _record_audit(
            audit_sink,
            organization_id=organization_id,
            work_item_id=work_item_id,
            session_id=str(session_uuid) if session_uuid else canonical_session_id,
            agent_name=agent_name,
            command_for_audit=f"git commit -m {message!r} (in {path})",
            exit_code=exit_code,
        )
        return _format_result("commit", exit_code, result.get("stdout", ""), result.get("stderr", ""))

    return ToolSpec(
        name="git_commit",
        description=GIT_COMMIT_DESCRIPTION,
        parameters=GIT_COMMIT_PARAMETERS,
        handler=git_commit,
    )


def create_git_commit_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    audit_sink: Optional[AuditSink] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
):
    """Create the git_commit tool as a LangChain tool (backward-compatible wrapper)."""
    from .adapters import to_langchain_tools

    spec = create_git_commit_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
        audit_sink=audit_sink,
        organization_id=organization_id,
        work_item_id=work_item_id,
        agent_name=agent_name,
    )
    return to_langchain_tools([spec])[0]


GIT_PUSH_DESCRIPTION = """
Push committed changes to a remote.

Requires the sandbox to have network egress to the git host --
by default the sandbox has NO network access, so this will fail
unless the operator has explicitly configured egress for the
target host.

WARNING: `force=True` performs a force-push (`git push --force`),
which can permanently destroy remote commit history other clients
depend on. Default is `False`; only set `force=True` when you are
certain no one else's work on the remote branch would be lost.

Args:
    path: Path to the repository (default /workspace)
    remote: Remote name (default "origin")
    branch: Branch to push (default: current branch)
    token: Optional short-lived HTTPS personal access token, used
        only for this call
    ssh_key: Optional short-lived SSH private key text, used only
        for this call
    force: Force-push, overwriting remote history (default False --
        see WARNING above)

Returns:
    Confirmation message, or an error message
"""

GIT_PUSH_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the repository (default /workspace)",
            "default": DEFAULT_WORKSPACE,
        },
        "remote": {
            "type": "string",
            "description": 'Remote name (default "origin")',
            "default": "origin",
        },
        "branch": {
            "type": "string",
            "description": "Branch to push (default: current branch)",
        },
        "token": {
            "type": "string",
            "description": "Optional short-lived HTTPS personal access token, used only for this call",
        },
        "ssh_key": {
            "type": "string",
            "description": "Optional short-lived SSH private key text, used only for this call",
        },
        "force": {
            "type": "boolean",
            "description": "Force-push, overwriting remote history (default False)",
            "default": False,
        },
    },
    "required": [],
}


def create_git_push_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    audit_sink: Optional[AuditSink] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for git_push."""
    canonical_session_id, session_uuid = _tool_context(
        sandbox_manager, lazy_runtime, session_id, organization_id, work_item_id, agent_name
    )

    async def git_push(
        path: str = DEFAULT_WORKSPACE,
        remote: str = "origin",
        branch: Optional[str] = None,
        token: Optional[str] = None,
        ssh_key: Optional[str] = None,
        force: bool = False,
    ) -> str:
        path = (path or DEFAULT_WORKSPACE).strip() or DEFAULT_WORKSPACE
        remote = (remote or "origin").strip() or "origin"

        try:
            manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=canonical_session_id,
            )
        except Exception as e:
            logger.error(f"[git_push] Failed to resolve sandbox context: {e}", exc_info=True)
            return f"Error resolving sandbox: {str(e)}"

        argv = ["push"]
        if force:
            argv.append("--force")
        argv.append(remote)
        if branch:
            argv.append(branch)

        cred_path: Optional[str] = None
        env_prefix = ""
        extra_config: list[str] = []
        try:
            if token:
                header_value = _basic_auth_header_value(token)
                cred_path = await _write_temp_credential(
                    manager, resolved_session_id, _http_extra_header_gitconfig(header_value)
                )
                extra_config = ["-c", f"include.path={cred_path}"]
            elif ssh_key:
                key_content = ssh_key if ssh_key.endswith("\n") else ssh_key + "\n"
                cred_path = await _write_temp_credential(manager, resolved_session_id, key_content)
                env_prefix = (
                    f"GIT_SSH_COMMAND={_quote('ssh -i ' + cred_path + ' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new')} "
                )

            full_argv = extra_config + argv
            result = await _run_git(
                manager,
                resolved_session_id,
                path=path,
                argv=full_argv,
                env_prefix=env_prefix,
                timeout=NETWORK_OP_TIMEOUT_SEC,
            )
        except Exception as e:
            logger.error(f"[git_push] Error: {e}", exc_info=True)
            return f"Error pushing changes: {str(e)}"
        finally:
            if cred_path:
                await _delete_temp_credential(manager, resolved_session_id, cred_path)

        exit_code = result.get("exit_code", 0)
        await _record_audit(
            audit_sink,
            organization_id=organization_id,
            work_item_id=work_item_id,
            session_id=str(session_uuid) if session_uuid else canonical_session_id,
            agent_name=agent_name,
            command_for_audit=f"git push {remote} {branch or ''} (force={force})".strip(),
            exit_code=exit_code,
        )
        return _format_result("push", exit_code, result.get("stdout", ""), result.get("stderr", ""))

    return ToolSpec(
        name="git_push",
        description=GIT_PUSH_DESCRIPTION,
        parameters=GIT_PUSH_PARAMETERS,
        handler=git_push,
    )


def create_git_push_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    audit_sink: Optional[AuditSink] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
):
    """Create the git_push tool as a LangChain tool (backward-compatible wrapper)."""
    from .adapters import to_langchain_tools

    spec = create_git_push_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
        audit_sink=audit_sink,
        organization_id=organization_id,
        work_item_id=work_item_id,
        agent_name=agent_name,
    )
    return to_langchain_tools([spec])[0]


GIT_PULL_DESCRIPTION = """
Pull changes from a remote into the current branch.

Requires the sandbox to have network egress to the git host --
by default the sandbox has NO network access, so this will fail
unless the operator has explicitly configured egress for the
target host.

SECURITY: a malicious remote's `post-checkout`/`post-merge` hook,
if one is already present in the local repo from a prior clone,
WILL run as part of this operation -- git_pull does not sanitize
or disable existing hooks. Only pull into repositories you trust.

Args:
    path: Path to the repository (default /workspace)
    remote: Remote name (default "origin")
    branch: Branch to pull (default: current branch's upstream)
    token: Optional short-lived HTTPS personal access token, used
        only for this call
    ssh_key: Optional short-lived SSH private key text, used only
        for this call

Returns:
    Confirmation message, or an error message
"""

GIT_PULL_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the repository (default /workspace)",
            "default": DEFAULT_WORKSPACE,
        },
        "remote": {
            "type": "string",
            "description": 'Remote name (default "origin")',
            "default": "origin",
        },
        "branch": {
            "type": "string",
            "description": "Branch to pull (default: current branch's upstream)",
        },
        "token": {
            "type": "string",
            "description": "Optional short-lived HTTPS personal access token, used only for this call",
        },
        "ssh_key": {
            "type": "string",
            "description": "Optional short-lived SSH private key text, used only for this call",
        },
    },
    "required": [],
}


def create_git_pull_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for git_pull."""
    canonical_session_id, _ = _tool_context(sandbox_manager, lazy_runtime, session_id, None, None, None)

    async def git_pull(
        path: str = DEFAULT_WORKSPACE,
        remote: str = "origin",
        branch: Optional[str] = None,
        token: Optional[str] = None,
        ssh_key: Optional[str] = None,
    ) -> str:
        path = (path or DEFAULT_WORKSPACE).strip() or DEFAULT_WORKSPACE
        remote = (remote or "origin").strip() or "origin"

        try:
            manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=canonical_session_id,
            )
        except Exception as e:
            logger.error(f"[git_pull] Failed to resolve sandbox context: {e}", exc_info=True)
            return f"Error resolving sandbox: {str(e)}"

        argv = ["pull", remote]
        if branch:
            argv.append(branch)

        cred_path: Optional[str] = None
        env_prefix = ""
        extra_config: list[str] = []
        try:
            if token:
                header_value = _basic_auth_header_value(token)
                cred_path = await _write_temp_credential(
                    manager, resolved_session_id, _http_extra_header_gitconfig(header_value)
                )
                extra_config = ["-c", f"include.path={cred_path}"]
            elif ssh_key:
                key_content = ssh_key if ssh_key.endswith("\n") else ssh_key + "\n"
                cred_path = await _write_temp_credential(manager, resolved_session_id, key_content)
                env_prefix = (
                    f"GIT_SSH_COMMAND={_quote('ssh -i ' + cred_path + ' -o IdentitiesOnly=yes -o StrictHostKeyChecking=accept-new')} "
                )

            full_argv = extra_config + argv
            result = await _run_git(
                manager,
                resolved_session_id,
                path=path,
                argv=full_argv,
                env_prefix=env_prefix,
                timeout=NETWORK_OP_TIMEOUT_SEC,
            )
        except Exception as e:
            logger.error(f"[git_pull] Error: {e}", exc_info=True)
            return f"Error pulling changes: {str(e)}"
        finally:
            if cred_path:
                await _delete_temp_credential(manager, resolved_session_id, cred_path)

        return _format_result(
            "pull", result.get("exit_code", 0), result.get("stdout", ""), result.get("stderr", "")
        )

    return ToolSpec(
        name="git_pull",
        description=GIT_PULL_DESCRIPTION,
        parameters=GIT_PULL_PARAMETERS,
        handler=git_pull,
    )


def create_git_pull_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
):
    """Create the git_pull tool as a LangChain tool (backward-compatible wrapper)."""
    from .adapters import to_langchain_tools

    spec = create_git_pull_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]


GIT_BRANCH_DESCRIPTION = """
List branches, or create a new branch, in a git repository.

Args:
    path: Path to the repository (default /workspace)
    name: If provided, creates a new branch with this name instead
        of listing existing branches

Returns:
    Branch listing, or a confirmation message for the new branch
"""

GIT_BRANCH_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the repository (default /workspace)",
            "default": DEFAULT_WORKSPACE,
        },
        "name": {
            "type": "string",
            "description": "If provided, creates a new branch with this name instead of listing existing branches",
        },
    },
    "required": [],
}


def create_git_branch_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for git_branch."""
    canonical_session_id, _ = _tool_context(sandbox_manager, lazy_runtime, session_id, None, None, None)

    async def git_branch(path: str = DEFAULT_WORKSPACE, name: Optional[str] = None) -> str:
        path = (path or DEFAULT_WORKSPACE).strip() or DEFAULT_WORKSPACE
        argv = ["branch"] if not name else ["branch", name]

        try:
            manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=canonical_session_id,
            )
            result = await _run_git(manager, resolved_session_id, path=path, argv=argv)
        except Exception as e:
            logger.error(f"[git_branch] Error: {e}", exc_info=True)
            return f"Error with git branch: {str(e)}"

        return _format_result(
            "branch", result.get("exit_code", 0), result.get("stdout", ""), result.get("stderr", "")
        )

    return ToolSpec(
        name="git_branch",
        description=GIT_BRANCH_DESCRIPTION,
        parameters=GIT_BRANCH_PARAMETERS,
        handler=git_branch,
    )


def create_git_branch_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
):
    """Create the git_branch tool as a LangChain tool (backward-compatible wrapper)."""
    from .adapters import to_langchain_tools

    spec = create_git_branch_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]


GIT_CHECKOUT_DESCRIPTION = """
Check out a branch or commit in a git repository.

SECURITY: a malicious repo's `post-checkout` hook, if one is
already present from a prior clone, WILL run as part of this
operation -- git_checkout does not sanitize or disable existing
hooks. Only check out refs in repositories you trust.

Args:
    path: Path to the repository (default /workspace)
    ref: Branch name, tag, or commit to check out (required)
    create: If True, creates `ref` as a new branch (`git checkout -b`)

Returns:
    Confirmation message, or an error message
"""

GIT_CHECKOUT_PARAMETERS = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the repository (default /workspace)",
            "default": DEFAULT_WORKSPACE,
        },
        "ref": {
            "type": "string",
            "description": "Branch name, tag, or commit to check out (required)",
            "default": "",
        },
        "create": {
            "type": "boolean",
            "description": "If True, creates `ref` as a new branch (`git checkout -b`)",
            "default": False,
        },
    },
    "required": [],
}


def create_git_checkout_tool_spec(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
) -> ToolSpec:
    """Build the framework-agnostic ToolSpec for git_checkout."""
    canonical_session_id, _ = _tool_context(sandbox_manager, lazy_runtime, session_id, None, None, None)

    async def git_checkout(path: str = DEFAULT_WORKSPACE, ref: str = "", create: bool = False) -> str:
        path = (path or DEFAULT_WORKSPACE).strip() or DEFAULT_WORKSPACE
        if not ref or not ref.strip():
            return "Error: ref is required"
        ref = ref.strip()

        argv = ["checkout"]
        if create:
            argv.append("-b")
        argv.append(ref)

        try:
            manager, resolved_session_id = await resolve_sandbox_operation_context(
                lazy_runtime=lazy_runtime,
                sandbox_manager=sandbox_manager,
                session_id=canonical_session_id,
            )
            result = await _run_git(manager, resolved_session_id, path=path, argv=argv)
        except Exception as e:
            logger.error(f"[git_checkout] Error: {e}", exc_info=True)
            return f"Error checking out ref: {str(e)}"

        return _format_result(
            "checkout", result.get("exit_code", 0), result.get("stdout", ""), result.get("stderr", "")
        )

    return ToolSpec(
        name="git_checkout",
        description=GIT_CHECKOUT_DESCRIPTION,
        parameters=GIT_CHECKOUT_PARAMETERS,
        handler=git_checkout,
    )


def create_git_checkout_tool(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
):
    """Create the git_checkout tool as a LangChain tool (backward-compatible wrapper)."""
    from .adapters import to_langchain_tools

    spec = create_git_checkout_tool_spec(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    return to_langchain_tools([spec])[0]


def create_git_tool_specs(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    audit_sink: Optional[AuditSink] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
) -> list[ToolSpec]:
    """
    Build the full, framework-agnostic git ToolSpec set: clone, status, add,
    commit, push, pull, branch, checkout.

    Mirrors create_sandbox_tool_specs()'s per-operation wiring so callers can
    opt a specific agent into git tools by including this list's output
    (rather than every tool being force-included) -- see
    docs/GIT-OPERATIONS-DESIGN.md §2.2's read-only-agent allowlist rationale.
    """
    common_kwargs = dict(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
    )
    audited_kwargs = dict(
        common_kwargs,
        audit_sink=audit_sink,
        organization_id=organization_id,
        work_item_id=work_item_id,
        agent_name=agent_name,
    )
    return [
        create_git_clone_tool_spec(**audited_kwargs),
        create_git_status_tool_spec(**common_kwargs),
        create_git_add_tool_spec(**common_kwargs),
        create_git_commit_tool_spec(**audited_kwargs),
        create_git_push_tool_spec(**audited_kwargs),
        create_git_pull_tool_spec(**common_kwargs),
        create_git_branch_tool_spec(**common_kwargs),
        create_git_checkout_tool_spec(**common_kwargs),
    ]


def create_git_tools(
    sandbox_manager: Optional["SandboxManager"] = None,
    session_id: Optional[str] = None,
    lazy_runtime: Optional["LazySandboxRuntime"] = None,
    audit_sink: Optional[AuditSink] = None,
    organization_id: Optional[UUID] = None,
    work_item_id: Optional[UUID] = None,
    agent_name: Optional[str] = None,
) -> list:
    """
    Create the full git tool set as LangChain tools (backward-compatible
    wrapper): clone, status, add, commit, push, pull, branch, checkout.

    Prefer `create_git_tool_specs()` for framework-agnostic use -- this
    function just adapts those specs via
    boxkite.tools.adapters.to_langchain_tools. Requires the `langchain`
    extra.
    """
    from .adapters import to_langchain_tools

    specs = create_git_tool_specs(
        sandbox_manager=sandbox_manager,
        session_id=session_id,
        lazy_runtime=lazy_runtime,
        audit_sink=audit_sink,
        organization_id=organization_id,
        work_item_id=work_item_id,
        agent_name=agent_name,
    )
    return to_langchain_tools(specs)
