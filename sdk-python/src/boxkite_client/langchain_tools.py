"""LangChain tool factory over a hosted boxkite control-plane.

Mirrors boxkite.tools' own tool names and shapes (bash_tool, file_create,
view, str_replace, ls, glob, grep) so an agent switching between a
self-hosted deployment (boxkite.tools.create_sandbox_tools, embedding
SandboxManager directly) and this hosted client needs no changes beyond
which factory it calls. Requires the `langchain-core` extra:
`pip install boxkite-client[langchain]`.
"""

from __future__ import annotations

from langchain_core.tools import tool

from .client import BoxkiteClient
from .exceptions import BoxkiteApiError, BoxkiteConnectionError


def create_sandbox_tools(client: BoxkiteClient, session_id: str) -> list:
    """Create the twelve sandbox tools (bash_tool, file_create, view,
    str_replace, ls, glob, grep, start_process, get_process_output,
    send_process_input, stop_process, list_processes) bound to one
    session_id on one client.

    Each tool catches BoxkiteApiError and returns a descriptive string
    instead of raising -- an uncaught exception here would kill whatever
    agent loop is calling the tool, and "the sandbox call failed" is
    exactly the kind of thing an agent should be able to see and react to.
    """

    @tool
    def bash_tool(command: str) -> str:
        """Run a shell command in the sandbox. Returns stdout, or stderr
        and the exit code if the command failed."""
        try:
            result = client.exec(session_id, command)
        except (BoxkiteApiError, BoxkiteConnectionError) as exc:
            return f"Error running command: {getattr(exc, 'message', str(exc))}"
        if result["exit_code"] != 0:
            return f"Command exited {result['exit_code']}. stdout:\n{result['stdout']}\nstderr:\n{result['stderr']}"
        return result["stdout"]

    @tool
    def file_create(path: str, content: str) -> str:
        """Create or overwrite a file in the sandbox workspace."""
        try:
            result = client.file_create(session_id, path, content)
        except (BoxkiteApiError, BoxkiteConnectionError) as exc:
            return f"Error creating file: {getattr(exc, 'message', str(exc))}"
        return f"Wrote {result.get('path', path)} ({result.get('size', len(content))} bytes)"

    @tool
    def view(path: str) -> str:
        """View a file's contents, or list a directory's entries."""
        try:
            result = client.view(session_id, path)
        except (BoxkiteApiError, BoxkiteConnectionError) as exc:
            return f"Error viewing {path}: {getattr(exc, 'message', str(exc))}"
        return result.get("content") or str(result)

    @tool
    def str_replace(path: str, old_str: str, new_str: str) -> str:
        """Replace a unique string in a sandbox file. old_str must appear
        exactly once in the file."""
        try:
            result = client.str_replace(session_id, path, old_str, new_str)
        except (BoxkiteApiError, BoxkiteConnectionError) as exc:
            return f"Error editing {path}: {getattr(exc, 'message', str(exc))}"
        return f"Replaced in {result.get('path', path)} ({result.get('occurrences', 1)} replacement(s))"

    @tool
    def ls(path: str = "/") -> str:
        """List the direct children of a directory in the sandbox workspace."""
        try:
            result = client.ls(session_id, path=path)
        except (BoxkiteApiError, BoxkiteConnectionError) as exc:
            return f"Error listing {path}: {getattr(exc, 'message', str(exc))}"
        entries = result.get("entries", [])
        if not entries:
            return f"{path} is empty."
        return "\n".join(str(e) for e in entries)

    @tool
    def glob(pattern: str, path: str = "/") -> str:
        """Find files by name pattern (e.g. '**/*.py') under a directory in the sandbox workspace."""
        try:
            result = client.glob(session_id, pattern, path=path)
        except (BoxkiteApiError, BoxkiteConnectionError) as exc:
            return f"Error matching {pattern}: {getattr(exc, 'message', str(exc))}"
        matches = result.get("matches", [])
        if not matches:
            return f"No files matched {pattern!r} under {path}."
        return "\n".join(str(m) for m in matches)

    @tool
    def grep(pattern: str, path: str = "/", glob: str | None = None, max_matches: int = 500) -> str:
        """Search file contents by regex pattern under a directory in the sandbox workspace,
        optionally restricted to files matching a glob."""
        try:
            result = client.grep(session_id, pattern, path=path, glob=glob, max_matches=max_matches)
        except (BoxkiteApiError, BoxkiteConnectionError) as exc:
            return f"Error searching for {pattern}: {getattr(exc, 'message', str(exc))}"
        if result.get("error"):
            return f"Error searching for {pattern}: {result['error']}"
        matches = result.get("matches", [])
        if not matches:
            return f"No matches for {pattern!r} under {path}."
        suffix = " (truncated)" if result.get("truncated") else ""
        return "\n".join(str(m) for m in matches) + suffix

    @tool
    def start_process(command: str, description: str | None = None, max_runtime_seconds: int = 3600) -> str:
        """Start a long-running background process in the sandbox (a dev
        server, a test watcher, a long build, a REPL) that keeps running
        after this tool call returns. Distinct from bash_tool, which is
        one-shot and bounded by its own timeout."""
        try:
            result = client.start_process(
                session_id, command, description=description, max_runtime_seconds=max_runtime_seconds
            )
        except (BoxkiteApiError, BoxkiteConnectionError) as exc:
            return f"Error starting process: {getattr(exc, 'message', str(exc))}"
        return (
            f"Started process {result.get('process_id')} (status={result.get('status')}). "
            f"Use get_process_output(\"{result.get('process_id')}\") to check on it."
        )

    @tool
    def get_process_output(process_id: str, since_offset: int = 0) -> str:
        """Poll a background process's output since a given byte offset.
        Call repeatedly to watch a process's progress."""
        try:
            result = client.get_process_output(session_id, process_id, since_offset=since_offset)
        except (BoxkiteApiError, BoxkiteConnectionError) as exc:
            return f"Error getting process output: {getattr(exc, 'message', str(exc))}"
        lines = [f"status: {result.get('status')}"]
        if result.get("exit_code") is not None:
            lines.append(f"exit_code: {result.get('exit_code')}")
        if result.get("truncated"):
            lines.append("(earlier output was truncated -- the buffer only keeps recent output)")
        lines.append(f"next_offset: {result.get('next_offset')}")
        lines.append("--- output ---")
        lines.append(result.get("stdout_chunk") or "(no new output)")
        return "\n".join(lines)

    @tool
    def send_process_input(process_id: str, data: str) -> str:
        """Write to a background process's stdin (e.g. answering an
        interactive prompt in a REPL)."""
        try:
            result = client.send_process_input(session_id, process_id, data)
        except (BoxkiteApiError, BoxkiteConnectionError) as exc:
            return f"Error sending input to process: {getattr(exc, 'message', str(exc))}"
        return f"Wrote {result.get('bytes_written')} bytes to process {process_id}"

    @tool
    def stop_process(process_id: str) -> str:
        """Stop a background process (SIGTERM, then SIGKILL if it doesn't
        exit within a few seconds)."""
        try:
            result = client.stop_process(session_id, process_id)
        except (BoxkiteApiError, BoxkiteConnectionError) as exc:
            return f"Error stopping process: {getattr(exc, 'message', str(exc))}"
        return f"Process {process_id}: {result.get('status')} (exit_code={result.get('exit_code')})"

    @tool
    def list_processes() -> str:
        """List every background process currently tracked in this sandbox session."""
        try:
            result = client.list_processes(session_id)
        except (BoxkiteApiError, BoxkiteConnectionError) as exc:
            return f"Error listing processes: {getattr(exc, 'message', str(exc))}"
        processes = result.get("processes", [])
        if not processes:
            return "(no background processes)"
        lines = []
        for proc in processes:
            desc = f" ({proc.get('description')})" if proc.get("description") else ""
            exit_code = proc.get("exit_code")
            exit_str = f", exit_code={exit_code}" if exit_code is not None else ""
            lines.append(f"{proc.get('process_id')}{desc}: {proc.get('status')}{exit_str} -- {proc.get('command')}")
        return "\n".join(lines)

    return [
        bash_tool,
        file_create,
        view,
        str_replace,
        ls,
        glob,
        grep,
        start_process,
        get_process_output,
        send_process_input,
        stop_process,
        list_processes,
    ]
