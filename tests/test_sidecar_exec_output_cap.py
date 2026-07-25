"""Tests for Critical: unbounded stdout/stderr buffering in exec_in_sandbox.

`asyncio.subprocess.Process.communicate()` buffers the entire stream in
memory before the old code truncated to 1MB/256KB — a command that writes
gigabytes of output (e.g. `cat /dev/zero`) could OOM-kill the sidecar pod
before truncation ever happened. `_read_stream_bounded` reads incrementally
and kills the process the instant the cap is hit, so memory usage is capped
at `max_bytes` regardless of how much the process tries to write.
"""

import asyncio

import main as sidecar_main


class _InfiniteStream:
    """Fake StreamReader that yields chunks forever until told to stop."""

    def __init__(self, chunk: bytes):
        self._chunk = chunk
        self.stopped = False

    async def read(self, n: int) -> bytes:
        if self.stopped:
            return b""
        return self._chunk[:n] if len(self._chunk) >= n else self._chunk


class _FakeProc:
    def __init__(self):
        self.returncode = None
        self.killed = False

    def kill(self):
        self.killed = True
        self.returncode = -9


async def test_read_stream_bounded_stops_at_cap_and_kills_process():
    stream = _InfiniteStream(b"a" * 8192)
    proc = _FakeProc()
    max_bytes = 20_000

    result = await sidecar_main._read_stream_bounded(stream, max_bytes, proc)

    assert len(result) == max_bytes
    assert proc.killed is True


async def test_read_stream_bounded_does_not_kill_when_under_cap():
    class _FiniteStream:
        def __init__(self, data: bytes):
            self._remaining = data

        async def read(self, n: int) -> bytes:
            chunk, self._remaining = self._remaining[:n], self._remaining[n:]
            return chunk

    proc = _FakeProc()
    data = b"hello world"
    result = await sidecar_main._read_stream_bounded(_FiniteStream(data), 1024, proc)

    assert result == data
    assert proc.killed is False


async def test_exec_in_sandbox_kills_process_that_exceeds_output_cap(monkeypatch):
    """End-to-end via a real subprocess: writing well past the 1MB stdout cap
    must be truncated at exactly the cap and the process must be killed
    rather than left to finish writing (or OOM the host)."""
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "compose")
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 4242)
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def _fake_create_subprocess_exec(*cmd, **kwargs):
        # 5MB of 'x' — comfortably over the 1MB cap but cheap/fast to generate.
        return await real_create_subprocess_exec(
            "python3",
            "-c",
            "import sys; sys.stdout.buffer.write(b'x' * (5 * 1024 * 1024))",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    monkeypatch.setattr(sidecar_main.asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    exit_code, stdout, stderr = await sidecar_main.exec_in_sandbox("irrelevant", timeout=10)

    assert len(stdout.encode("utf-8")) == sidecar_main.EXEC_MAX_STDOUT_BYTES
    assert exit_code != 0
