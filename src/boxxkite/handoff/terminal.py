"""Local terminal passthrough: puts the caller's own stdin into raw mode
and bridges it bidirectionally with an open takeover websocket -- a
terminal-native version of the same duplex a browser xterm.js client
already does against this same /pty channel (docs/SANDBOX-OBSERVABILITY-
DESIGN.md).
"""

from __future__ import annotations

import sys
import termios
import tty
from threading import Thread
from typing import Any

READ_CHUNK_SIZE = 4096


def run_terminal_passthrough(ws: Any) -> None:
    """Blocks until the sandbox side closes the connection or local stdin
    hits EOF. Restores the terminal's original settings on the way out
    even if the sandbox side errors."""
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        reader = Thread(target=_pump_stdin_to_ws, args=(ws,), daemon=True)
        reader.start()
        _pump_ws_to_stdout(ws)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)


def _pump_stdin_to_ws(ws: Any, stdin_buffer: Any = None) -> None:
    stdin_buffer = stdin_buffer if stdin_buffer is not None else sys.stdin.buffer
    while True:
        chunk = stdin_buffer.read1(READ_CHUNK_SIZE)
        if not chunk:
            break
        try:
            ws.send(chunk)
        except Exception:
            break


def _pump_ws_to_stdout(ws: Any) -> None:
    for message in ws:
        data = message if isinstance(message, bytes) else message.encode("utf-8")
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
