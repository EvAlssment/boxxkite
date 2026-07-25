from __future__ import annotations

from boxkite_handoff.terminal import _pump_stdin_to_ws, _pump_ws_to_stdout


class FakeWebsocket:
    def __init__(self, incoming: list[bytes] | None = None) -> None:
        self.sent: list[bytes] = []
        self._incoming = incoming or []

    def send(self, data: bytes) -> None:
        self.sent.append(data)

    def __iter__(self):
        return iter(self._incoming)


class FakeStdinBuffer:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = list(chunks)

    def read1(self, _size: int) -> bytes:
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


def test_pump_stdin_to_ws_forwards_every_chunk_until_eof() -> None:
    ws = FakeWebsocket()
    fake_stdin = FakeStdinBuffer([b"hello", b" world", b""])

    _pump_stdin_to_ws(ws, fake_stdin)

    assert ws.sent == [b"hello", b" world"]


def test_pump_stdin_to_ws_stops_on_send_error() -> None:
    class ExplodingWebsocket(FakeWebsocket):
        def send(self, data: bytes) -> None:
            raise RuntimeError("connection closed")

    ws = ExplodingWebsocket()
    fake_stdin = FakeStdinBuffer([b"hello", b"never reached"])

    _pump_stdin_to_ws(ws, fake_stdin)  # must not raise


def test_pump_ws_to_stdout_writes_every_message(monkeypatch, capsys) -> None:
    ws = FakeWebsocket(incoming=[b"binary", "text-message"])

    _pump_ws_to_stdout(ws)

    captured = capsys.readouterr()
    assert captured.out == "binarytext-message"
