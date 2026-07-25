"""
Tests for boxkite.tools.git_tools.

Mirrors tests/test_bash_tool.py's and tests/test_search_tools.py's pattern:
mock SandboxManager (execute + file_create), assert the tool builds the
right git invocation, and assert the credential-handling properties
docs/GIT-OPERATIONS-DESIGN.md §5 calls out as load-bearing:

- Credentials are written only under /tmp, never /workspace or
  /mnt/user-data/outputs.
- The literal credential value never appears in the command string handed
  to SandboxManager.execute() (git reads it directly from the /tmp file
  itself, via `-c include.path=<path>` for HTTPS tokens or an `-i <path>`
  SSH flag -- never through a shell command substitution).
- The temp credential file is deleted after exactly one git invocation,
  on both the success and failure path.
"""

from uuid import uuid4

import pytest

from boxkite.tools.bash_tool import sanitize_output
from boxkite.tools.git_tools import (
    create_git_add_tool,
    create_git_branch_tool,
    create_git_checkout_tool,
    create_git_clone_tool,
    create_git_commit_tool,
    create_git_pull_tool,
    create_git_push_tool,
    create_git_status_tool,
    create_git_tools,
)

pytestmark = pytest.mark.pr


class _FakeSandboxManager:
    """Records every execute()/file_create() call in call order.

    `exec_results` lets a test script per-call exit codes; if the queue is
    exhausted the last entry repeats.
    """

    def __init__(self, exec_results=None):
        self.execute_calls = []
        self.file_create_calls = []
        self._exec_results = list(exec_results) if exec_results else [
            {"exit_code": 0, "stdout": "ok", "stderr": ""}
        ]

    async def execute(self, session_id, command, timeout):
        self.execute_calls.append(
            {"session_id": session_id, "command": command, "timeout": timeout}
        )
        if len(self._exec_results) > 1:
            return self._exec_results.pop(0)
        return self._exec_results[0]

    async def file_create(self, session_id, path, content, description=None):
        self.file_create_calls.append(
            {"session_id": session_id, "path": path, "content": content}
        )
        return {"path": path, "size": len(content)}


class _RecordingAuditSink:
    def __init__(self):
        self.record_exec_calls = []

    async def record_exec(self, **kwargs):
        self.record_exec_calls.append(kwargs)


def _all_command_text(manager: _FakeSandboxManager) -> str:
    return "\n".join(call["command"] for call in manager.execute_calls)


# ---------------------------------------------------------------------------
# Construction / wiring
# ---------------------------------------------------------------------------

def test_create_git_clone_tool_requires_a_manager_or_lazy_runtime():
    with pytest.raises(ValueError, match="sandbox_manager must be provided"):
        create_git_clone_tool()


def test_create_git_tools_returns_all_eight_operations():
    manager = _FakeSandboxManager()
    tools = create_git_tools(session_id="session-1", sandbox_manager=manager)
    names = {t.name for t in tools}
    assert names == {
        "git_clone",
        "git_status",
        "git_add",
        "git_commit",
        "git_push",
        "git_pull",
        "git_branch",
        "git_checkout",
    }
    assert len(tools) == 8


# ---------------------------------------------------------------------------
# git_clone
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_git_clone_rejects_file_url():
    manager = _FakeSandboxManager()
    tool = create_git_clone_tool(session_id="session-1", sandbox_manager=manager)

    result = await tool.ainvoke({"url": "file:///etc/passwd", "path": "/workspace/repo"})

    assert "https://" in result or "url must use" in result
    assert len(manager.execute_calls) == 0


@pytest.mark.asyncio
async def test_git_clone_rejects_empty_url():
    manager = _FakeSandboxManager()
    tool = create_git_clone_tool(session_id="session-1", sandbox_manager=manager)

    result = await tool.ainvoke({"url": "", "path": "/workspace/repo"})

    assert "url is required" in result


@pytest.mark.asyncio
async def test_git_clone_without_credentials_builds_plain_clone_command():
    manager = _FakeSandboxManager()
    tool = create_git_clone_tool(session_id="session-1", sandbox_manager=manager)

    result = await tool.ainvoke(
        {"url": "https://github.com/org/repo.git", "path": "/workspace/repo", "branch": "main", "depth": 1}
    )

    assert "completed successfully" in result or "ok" in result
    assert len(manager.execute_calls) == 1
    command = manager.execute_calls[0]["command"]
    assert "git clone" in command
    assert "--no-local" in command
    assert "--depth 1" in command
    assert "--branch main" in command
    assert "github.com/org/repo.git" in command
    # No credential file should have been written for a token-less clone.
    assert len(manager.file_create_calls) == 0


@pytest.mark.asyncio
async def test_git_clone_never_recurses_submodules():
    manager = _FakeSandboxManager()
    tool = create_git_clone_tool(session_id="session-1", sandbox_manager=manager)

    await tool.ainvoke({"url": "https://github.com/org/repo.git"})

    assert "--recurse-submodules" not in manager.execute_calls[0]["command"]


@pytest.mark.asyncio
async def test_git_clone_with_token_writes_credential_only_to_tmp():
    manager = _FakeSandboxManager(
        exec_results=[
            {"exit_code": 0, "stdout": "", "stderr": ""},  # chmod
            {"exit_code": 0, "stdout": "Cloned", "stderr": ""},  # git clone
            {"exit_code": 0, "stdout": "", "stderr": ""},  # rm cleanup
        ]
    )
    tool = create_git_clone_tool(session_id="session-1", sandbox_manager=manager)

    await tool.ainvoke(
        {"url": "https://github.com/org/repo.git", "path": "/workspace/repo", "token": "super-secret-token"}
    )

    assert len(manager.file_create_calls) == 1
    written_path = manager.file_create_calls[0]["path"]
    assert written_path.startswith("/tmp/")
    assert "/workspace" not in written_path
    assert "/mnt/user-data" not in written_path

    # The raw token must never appear in any command string sent to exec.
    combined = _all_command_text(manager)
    assert "super-secret-token" not in combined
    # No shell command substitution anywhere -- git reads the header value
    # from the /tmp gitconfig file itself via `-c include.path=<path>`.
    assert "$(cat" not in combined
    assert f"include.path={written_path}" in combined

    # The written file is a real gitconfig snippet git can `include.path`,
    # not a raw value meant for shell expansion.
    written_content = manager.file_create_calls[0]["content"]
    assert written_content.startswith("[http]")
    assert "extraHeader = Authorization: Basic " in written_content

    # The credential file must be deleted after the single clone call.
    rm_calls = [c["command"] for c in manager.execute_calls if c["command"].startswith("rm -f")]
    assert len(rm_calls) == 1
    assert written_path in rm_calls[0]


@pytest.mark.asyncio
async def test_git_clone_with_shell_metacharacter_laden_token_is_never_shell_interpreted():
    """A token containing backticks, `$(...)`, semicolons, and quotes must
    never be able to trigger shell command execution anywhere in the
    clone flow -- not in the exec command string, and not via the
    gitconfig file git reads back (base64-encoding the header value
    up front guarantees this; see `_http_extra_header_gitconfig`)."""
    manager = _FakeSandboxManager(
        exec_results=[
            {"exit_code": 0, "stdout": "", "stderr": ""},  # chmod
            {"exit_code": 0, "stdout": "Cloned", "stderr": ""},  # git clone
            {"exit_code": 0, "stdout": "", "stderr": ""},  # rm cleanup
        ]
    )
    tool = create_git_clone_tool(session_id="session-1", sandbox_manager=manager)

    malicious_token = '`touch /tmp/pwned` $(touch /tmp/pwned2); rm -rf /tmp/x; "quoted"\'s'
    await tool.ainvoke(
        {
            "url": "https://github.com/org/repo.git",
            "path": "/workspace/repo",
            "token": malicious_token,
        }
    )

    combined = _all_command_text(manager)
    # None of the raw shell metacharacters from the token reach any
    # command string -- the token is base64-encoded before it's ever
    # written anywhere, and only the credential *path* appears in argv.
    assert "touch" not in combined
    assert "rm -rf /tmp/x" not in combined
    assert malicious_token not in combined

    # The written gitconfig content is the base64-encoded header value,
    # not the raw token -- so even a hostile config-file parser sees only
    # base64-alphabet characters, never the shell metacharacters.
    written_content = manager.file_create_calls[0]["content"]
    assert "touch" not in written_content
    assert "rm -rf" not in written_content
    assert written_content.startswith("[http]\n\textraHeader = Authorization: Basic ")

    import base64

    header_line = written_content.splitlines()[1].strip()
    b64_value = header_line.split("Basic ", 1)[1]
    decoded = base64.b64decode(b64_value).decode("utf-8")
    assert decoded == f"x-access-token:{malicious_token}"


@pytest.mark.asyncio
async def test_git_clone_with_ssh_key_uses_git_ssh_command_and_cleans_up():
    manager = _FakeSandboxManager(
        exec_results=[
            {"exit_code": 0, "stdout": "", "stderr": ""},  # chmod
            {"exit_code": 0, "stdout": "Cloned", "stderr": ""},  # git clone
            {"exit_code": 0, "stdout": "", "stderr": ""},  # rm cleanup
        ]
    )
    tool = create_git_clone_tool(session_id="session-1", sandbox_manager=manager)

    ssh_key = "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKEFAKEFAKE\n-----END OPENSSH PRIVATE KEY-----"
    await tool.ainvoke(
        {"url": "git@github.com:org/repo.git", "path": "/workspace/repo", "ssh_key": ssh_key}
    )

    assert len(manager.file_create_calls) == 1
    written_path = manager.file_create_calls[0]["path"]
    assert written_path.startswith("/tmp/")
    assert manager.file_create_calls[0]["content"].strip() == ssh_key.strip()

    combined = _all_command_text(manager)
    assert "FAKEFAKEFAKE" not in combined
    assert "GIT_SSH_COMMAND=" in combined
    assert written_path in combined

    rm_calls = [c["command"] for c in manager.execute_calls if c["command"].startswith("rm -f")]
    assert len(rm_calls) == 1


@pytest.mark.asyncio
async def test_git_clone_deletes_credential_even_when_clone_fails():
    manager = _FakeSandboxManager(
        exec_results=[
            {"exit_code": 0, "stdout": "", "stderr": ""},  # chmod
            {"exit_code": 128, "stdout": "", "stderr": "fatal: authentication failed"},  # git clone fails
            {"exit_code": 0, "stdout": "", "stderr": ""},  # rm cleanup
        ]
    )
    tool = create_git_clone_tool(session_id="session-1", sandbox_manager=manager)

    result = await tool.ainvoke(
        {"url": "https://github.com/org/repo.git", "path": "/workspace/repo", "token": "bad-token"}
    )

    assert "Error" in result
    assert "bad-token" not in result
    rm_calls = [c["command"] for c in manager.execute_calls if c["command"].startswith("rm -f")]
    assert len(rm_calls) == 1, "credential file must be deleted even on failure"


@pytest.mark.asyncio
async def test_git_clone_mirrors_to_audit_sink_without_credentials():
    manager = _FakeSandboxManager()
    sink = _RecordingAuditSink()
    org_id = uuid4()
    work_item_id = uuid4()

    tool = create_git_clone_tool(
        session_id="session-1",
        sandbox_manager=manager,
        audit_sink=sink,
        organization_id=org_id,
        work_item_id=work_item_id,
        agent_name="researcher",
    )

    await tool.ainvoke(
        {"url": "https://github.com/org/repo.git", "path": "/workspace/repo", "token": "super-secret-token"}
    )

    assert len(sink.record_exec_calls) == 1
    call = sink.record_exec_calls[0]
    assert call["organization_id"] == org_id
    assert call["work_item_id"] == work_item_id
    assert "super-secret-token" not in call["command"]
    assert call["exit_code"] == 0


# ---------------------------------------------------------------------------
# git_status / git_add / git_commit / git_branch / git_checkout (local ops)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_git_status_builds_expected_command():
    manager = _FakeSandboxManager()
    tool = create_git_status_tool(session_id="session-1", sandbox_manager=manager)

    await tool.ainvoke({"path": "/workspace/repo"})

    command = manager.execute_calls[0]["command"]
    assert "cd /workspace/repo" in command
    assert "git status --porcelain=v1 --branch" in command


@pytest.mark.asyncio
async def test_git_add_defaults_to_add_all():
    manager = _FakeSandboxManager()
    tool = create_git_add_tool(session_id="session-1", sandbox_manager=manager)

    await tool.ainvoke({"path": "/workspace/repo"})

    assert "git add -A" in manager.execute_calls[0]["command"]


@pytest.mark.asyncio
async def test_git_add_with_specific_files():
    manager = _FakeSandboxManager()
    tool = create_git_add_tool(session_id="session-1", sandbox_manager=manager)

    await tool.ainvoke({"path": "/workspace/repo", "files": ["a.py", "b.py"]})

    command = manager.execute_calls[0]["command"]
    assert "git add a.py b.py" in command


@pytest.mark.asyncio
async def test_git_add_rejects_non_string_files():
    # The `files: list[str]` type annotation is enforced by the tool's own
    # pydantic args schema before the function body ever runs -- a non-
    # string entry never reaches SandboxManager.execute().
    from pydantic import ValidationError

    manager = _FakeSandboxManager()
    tool = create_git_add_tool(session_id="session-1", sandbox_manager=manager)

    with pytest.raises(ValidationError):
        await tool.ainvoke({"path": "/workspace/repo", "files": [123]})

    assert len(manager.execute_calls) == 0


@pytest.mark.asyncio
async def test_git_commit_requires_a_message():
    manager = _FakeSandboxManager()
    tool = create_git_commit_tool(session_id="session-1", sandbox_manager=manager)

    result = await tool.ainvoke({"message": "", "path": "/workspace/repo"})

    assert "message is required" in result
    assert len(manager.execute_calls) == 0


@pytest.mark.asyncio
async def test_git_commit_builds_expected_command_with_author_override():
    manager = _FakeSandboxManager()
    tool = create_git_commit_tool(session_id="session-1", sandbox_manager=manager)

    await tool.ainvoke(
        {
            "message": "fix bug",
            "path": "/workspace/repo",
            "author_name": "Agent",
            "author_email": "agent@example.com",
        }
    )

    command = manager.execute_calls[0]["command"]
    assert "user.name=Agent" in command
    assert "user.email=agent@example.com" in command
    assert "git commit -m 'fix bug'" in command or "fix bug" in command


@pytest.mark.asyncio
async def test_git_commit_mirrors_to_audit_sink():
    manager = _FakeSandboxManager()
    sink = _RecordingAuditSink()
    tool = create_git_commit_tool(session_id="session-1", sandbox_manager=manager, audit_sink=sink)

    await tool.ainvoke({"message": "fix bug", "path": "/workspace/repo"})

    assert len(sink.record_exec_calls) == 1
    assert "fix bug" in sink.record_exec_calls[0]["command"]


@pytest.mark.asyncio
async def test_git_branch_lists_by_default():
    manager = _FakeSandboxManager()
    tool = create_git_branch_tool(session_id="session-1", sandbox_manager=manager)

    await tool.ainvoke({"path": "/workspace/repo"})

    assert manager.execute_calls[0]["command"].strip().endswith("git branch")


@pytest.mark.asyncio
async def test_git_branch_creates_named_branch():
    manager = _FakeSandboxManager()
    tool = create_git_branch_tool(session_id="session-1", sandbox_manager=manager)

    await tool.ainvoke({"path": "/workspace/repo", "name": "feature-x"})

    assert "git branch feature-x" in manager.execute_calls[0]["command"]


@pytest.mark.asyncio
async def test_git_checkout_requires_a_ref():
    manager = _FakeSandboxManager()
    tool = create_git_checkout_tool(session_id="session-1", sandbox_manager=manager)

    result = await tool.ainvoke({"path": "/workspace/repo", "ref": ""})

    assert "ref is required" in result


@pytest.mark.asyncio
async def test_git_checkout_with_create_flag():
    manager = _FakeSandboxManager()
    tool = create_git_checkout_tool(session_id="session-1", sandbox_manager=manager)

    await tool.ainvoke({"path": "/workspace/repo", "ref": "feature-x", "create": True})

    assert "git checkout -b feature-x" in manager.execute_calls[0]["command"]


# ---------------------------------------------------------------------------
# git_push / git_pull (remote ops needing credentials/network)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_git_push_defaults_to_no_force():
    manager = _FakeSandboxManager()
    tool = create_git_push_tool(session_id="session-1", sandbox_manager=manager)

    await tool.ainvoke({"path": "/workspace/repo", "remote": "origin", "branch": "main"})

    command = manager.execute_calls[0]["command"]
    assert "--force" not in command
    assert "git push origin main" in command


@pytest.mark.asyncio
async def test_git_push_force_flag_is_explicit():
    manager = _FakeSandboxManager()
    tool = create_git_push_tool(session_id="session-1", sandbox_manager=manager)

    await tool.ainvoke({"path": "/workspace/repo", "remote": "origin", "branch": "main", "force": True})

    assert "git push --force origin main" in manager.execute_calls[0]["command"]


@pytest.mark.asyncio
async def test_git_push_with_token_never_leaks_token_into_command_text():
    manager = _FakeSandboxManager(
        exec_results=[
            {"exit_code": 0, "stdout": "", "stderr": ""},  # chmod
            {"exit_code": 0, "stdout": "pushed", "stderr": ""},  # git push
            {"exit_code": 0, "stdout": "", "stderr": ""},  # rm cleanup
        ]
    )
    tool = create_git_push_tool(session_id="session-1", sandbox_manager=manager)

    await tool.ainvoke({"path": "/workspace/repo", "remote": "origin", "token": "push-secret"})

    combined = _all_command_text(manager)
    assert "push-secret" not in combined
    rm_calls = [c["command"] for c in manager.execute_calls if c["command"].startswith("rm -f")]
    assert len(rm_calls) == 1


@pytest.mark.asyncio
async def test_git_pull_builds_expected_command():
    manager = _FakeSandboxManager()
    tool = create_git_pull_tool(session_id="session-1", sandbox_manager=manager)

    await tool.ainvoke({"path": "/workspace/repo", "remote": "origin", "branch": "main"})

    assert "git pull origin main" in manager.execute_calls[0]["command"]


# ---------------------------------------------------------------------------
# Output-redaction backstop (docs/GIT-OPERATIONS-DESIGN.md §5)
# ---------------------------------------------------------------------------

def test_sanitize_output_redacts_ssh_private_key_block():
    leaked = (
        "some output\n"
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQ==\n"
        "-----END OPENSSH PRIVATE KEY-----\n"
        "more output"
    )

    sanitized = sanitize_output(leaked)

    assert "BEGIN OPENSSH PRIVATE KEY" not in sanitized
    assert "b3BlbnNzaC1rZXktdjEA" not in sanitized
    assert "[REDACTED_SSH_PRIVATE_KEY]" in sanitized


def test_sanitize_output_redacts_generic_pat_shaped_token():
    leaked = "auth failed for token abcd_ZZZZZZZZZZZZZZZZZZZZZZZZ in request"

    sanitized = sanitize_output(leaked)

    assert "abcd_ZZZZZZZZZZZZZZZZZZZZZZZZ" not in sanitized
    assert "[REDACTED_PAT]" in sanitized
