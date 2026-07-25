"""Adapter registry -- `boxkite-handoff <name>` looks up `name` here.

Adding a new tool: implement HandoffAdapter in its own module in this
package, then register an instance (or a zero-arg factory) below. See
docs/handoff-adapters.md for the full contract.
"""

from __future__ import annotations

from ..core import HandoffAdapter
from .claude_code import ClaudeCodeAdapter
from .codex import CodexAdapter
from .cursor import CursorAdapter
from .opencode import OpencodeAdapter

ADAPTERS: dict[str, type[HandoffAdapter]] = {}


def register(adapter_cls: type[HandoffAdapter]) -> type[HandoffAdapter]:
    ADAPTERS[adapter_cls.name] = adapter_cls
    return adapter_cls


register(ClaudeCodeAdapter)
register(CodexAdapter)
register(OpencodeAdapter)
register(CursorAdapter)
