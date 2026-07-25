"""Shared pytest fixtures.

`sidecar/main.py` is a standalone FastAPI service (its own Dockerfile and
requirements, deployed separately from the `boxkite` package) rather than a
module inside `src/boxkite`. To exercise it directly in-process with
FastAPI's TestClient, its containing directory needs to be on `sys.path` so
`import main` resolves to `sidecar/main.py` and not something else.
"""

import sys
from pathlib import Path

_SIDECAR_DIR = str(Path(__file__).resolve().parent.parent / "sidecar")
if _SIDECAR_DIR not in sys.path:
    sys.path.insert(0, _SIDECAR_DIR)

# Deliberately does NOT set env vars like RUNTIME_MODE here: this conftest is
# shared by every test module, including tests/test_manager.py, which relies
# on RUNTIME_MODE being unset (K8s mode) by default for SandboxManager/
# WarmPoolManager tests. sidecar/main.py has sane import-time defaults of its
# own (RUNTIME_MODE="k8s", STORAGE_BACKEND="s3") and its storage/K8s clients
# are constructed lazily, not at import time, so no env setup is needed just
# to import it. Individual sidecar tests that need a specific value
# monkeypatch the module attribute directly (e.g. `monkeypatch.setattr(
# sidecar_main, "SIDECAR_AUTH_TOKEN", ...)`) rather than re-importing.
