"""Shared pytest fixtures.

`sidecar/main.py` is a standalone FastAPI service (its own Dockerfile and
requirements, deployed separately from the `boxxkite` package) rather than a
module inside `src/boxxkite`. To exercise it directly in-process with
FastAPI's TestClient, its containing directory needs to be on `sys.path` so
`import main` resolves to `sidecar/main.py` and not something else.
"""

import shutil
import sys
from pathlib import Path

import pytest

_SIDECAR_DIR = str(Path(__file__).resolve().parent.parent / "sidecar")
if _SIDECAR_DIR not in sys.path:
    sys.path.insert(0, _SIDECAR_DIR)

# tmux backs the /pty takeover route's persistent session, and the sidecar's
# /configure route tears that session down as part of wiping state for the next
# tenant -- so `tmux` is a hard requirement for far more tests than just the
# pty ones. It's installed in deploy/sidecar.Dockerfile and preinstalled on
# GitHub's ubuntu runners, so CI never notices when it's absent; a contributor
# on a stock macOS box gets an opaque ExceptionGroup wrapping
# `FileNotFoundError: 'tmux'` instead of a skip. Shared here so every module
# that reaches tmux (directly, or transitively via /configure) guards the same
# way rather than each redefining it.
_TMUX_BIN = shutil.which("tmux")
requires_tmux = pytest.mark.skipif(
    _TMUX_BIN is None, reason="tmux is not installed on this test runner"
)

# Deliberately does NOT set env vars like RUNTIME_MODE here: this conftest is
# shared by every test module, including tests/test_manager.py, which relies
# on RUNTIME_MODE being unset (K8s mode) by default for SandboxManager/
# WarmPoolManager tests. sidecar/main.py has sane import-time defaults of its
# own (RUNTIME_MODE="k8s", STORAGE_BACKEND="s3") and its storage/K8s clients
# are constructed lazily, not at import time, so no env setup is needed just
# to import it. Individual sidecar tests that need a specific value
# monkeypatch the module attribute directly (e.g. `monkeypatch.setattr(
# sidecar_main, "SIDECAR_AUTH_TOKEN", ...)`) rather than re-importing.
