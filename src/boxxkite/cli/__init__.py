"""The `boxxkite` CLI: a two-minute path to using boxxkite locally
(`boxxkite up` + `boxxkite exec`) or against a hosted control-plane
(`boxxkite signup` + `boxxkite session create` + `boxxkite exec`), without
hand-writing curl calls or wiring `SandboxManager` into a LangChain agent
yourself.
"""

from .app import app

__all__ = ["app"]
