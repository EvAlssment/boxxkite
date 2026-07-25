"""The `boxkite` CLI: a two-minute path to using boxkite locally
(`boxkite up` + `boxkite exec`) or against a hosted control-plane
(`boxkite signup` + `boxkite session create` + `boxkite exec`), without
hand-writing curl calls or wiring `SandboxManager` into a LangChain agent
yourself.
"""

from .app import app

__all__ = ["app"]
