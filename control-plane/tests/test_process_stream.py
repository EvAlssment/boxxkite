"""Live SSE streaming of a background process's stdout
(GET /v1/sandboxes/{id}/processes/{pid}/stream)."""

from __future__ import annotations

import httpx

from conftest import FakeSandboxManager, signup_and_get_api_key
from control_plane.routers import sandboxes


class _StubRequest:
    async def is_disconnected(self) -> bool:
        return False


class _SequenceManager:
    """Returns a scripted sequence of get_process_output results."""

    def __init__(self, results):
        self._results = list(results)
        self._i = 0

    async def get_process_output(self, session_id, process_id, since_offset):
        result = self._results[min(self._i, len(self._results) - 1)]
        self._i += 1
        return result


class TestStreamGenerator:
    async def test_emits_each_chunk_then_exit(self, monkeypatch):
        monkeypatch.setattr(sandboxes, "SANDBOX_PROCESS_STREAM_POLL_INTERVAL_SECONDS", 0)
        first = {"status": "running", "stdout_chunk": "a", "next_offset": 1, "exit_code": None}
        mgr = _SequenceManager(
            [
                {"status": "running", "stdout_chunk": "b", "next_offset": 2, "exit_code": None},
                {"status": "exited", "stdout_chunk": "", "next_offset": 2, "exit_code": 0},
            ]
        )
        events = [
            ev
            async for ev in sandboxes._process_output_stream(
                _StubRequest(), mgr, session_id="s", process_id="p", first_result=first, start_offset=0
            )
        ]
        joined = "".join(events)
        assert joined.count("event: output") == 2
        assert '"stdout_chunk": "a"' in joined
        assert '"stdout_chunk": "b"' in joined
        assert "event: exit" in joined
        assert '"exit_code": 0' in joined

    async def test_stops_immediately_when_client_disconnected(self):
        class _Disconnected:
            async def is_disconnected(self):
                return True

        first = {"status": "running", "stdout_chunk": "x", "next_offset": 1, "exit_code": None}
        events = [
            ev
            async for ev in sandboxes._process_output_stream(
                _Disconnected(), _SequenceManager([]), session_id="s", process_id="p",
                first_result=first, start_offset=0,
            )
        ]
        assert events == []


async def _create_session(client, key) -> str:
    resp = await client.post("/v1/sandboxes", json={}, headers={"Authorization": f"Bearer {key}"})
    assert resp.status_code == 201
    return resp.json()["id"]


class TestStreamEndpoint:
    async def test_streams_output_and_exit_for_finished_process(
        self, client: httpx.AsyncClient, fake_manager: FakeSandboxManager
    ):
        key = await signup_and_get_api_key(client, "stream-ok@example.com")
        session_id = await _create_session(client, key)
        # Seed a already-finished process so the stream terminates deterministically.
        fake_manager._processes.setdefault(session_id, {})["proc_x"] = {
            "process_id": "proc_x",
            "status": "exited",
            "stdout": "hello from stream",
            "exit_code": 0,
        }

        resp = await client.get(
            f"/v1/sandboxes/{session_id}/processes/proc_x/stream",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 200
        assert "text/event-stream" in resp.headers["content-type"]
        body = resp.text
        assert "event: output" in body
        assert "hello from stream" in body
        assert "event: exit" in body
        assert '"exit_code": 0' in body

    async def test_unknown_process_is_404(
        self, client: httpx.AsyncClient, fake_manager: FakeSandboxManager
    ):
        key = await signup_and_get_api_key(client, "stream-404@example.com")
        session_id = await _create_session(client, key)
        resp = await client.get(
            f"/v1/sandboxes/{session_id}/processes/nope/stream",
            headers={"Authorization": f"Bearer {key}"},
        )
        assert resp.status_code == 404
