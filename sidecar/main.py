"""
Sandbox Sidecar - HTTP API for tool execution

This sidecar runs alongside the sandbox container in the same K8s pod.
All tool calls from the backend route through this HTTP API on :8080.

Endpoints:
- POST /exec - Execute bash command in sandbox (via nsenter)
- POST /interpreter/exec - Execute code in a persistent, kept-alive Python
  interpreter for the session (variables survive across calls, unlike /exec)
- POST /interpreter/reset - Kill the persistent interpreter
- GET /interpreter/status - Report whether the interpreter is running
- POST /node-interpreter/exec - Same as /interpreter/exec, but for a
  persistent Node.js process (404s unless BOXKITE_NODE_INTERPRETER_ENABLED)
- POST /node-interpreter/reset - Kill the persistent Node.js interpreter
- GET /node-interpreter/status - Report whether it is running
- POST /lsp/start - Start a persistent language server (pyright for Python,
  typescript-language-server for TypeScript/JS; 404s unless
  BOXKITE_LSP_ENABLED)
- POST /lsp/{id}/open - Open or full-document-replace a file on that server
- POST /lsp/{id}/completion - Request textDocument/completion at a position
- POST /lsp/{id}/stop - Gracefully shut down the language server
- POST /browser/navigate - Load a URL in the session's one headless-Chromium
  page (404s unless BOXKITE_BROWSER_ENABLED); lazily starts the browser
- POST /browser/exec - Evaluate a script in the current page's JS context
- POST /browser/screenshot - Capture the current page as a base64 PNG
- POST /browser/close - Tear down the browser process (idempotent)
- POST /file-create - Create file on shared volume
- POST /ensure-skills - Ensure immutable skill files exist under /mnt/skills
- POST /view - Read text file from shared volume
- POST /read-image - Read image file bytes from shared volume
- POST /str-replace - Edit file on shared volume
- POST /present-files - Ensure storage sync, return file info
- POST /process/start - Start a tracked background process (nsenter, like /exec)
- GET /process/{id}/output - Poll a background process's output since an offset
- POST /process/{id}/input - Write to a background process's stdin
- POST /process/{id}/stop - SIGTERM/SIGKILL a background process
- GET /process - List tracked background processes
- POST /process/kill-all - SIGKILL every tracked background process
- ANY /preview/{port}/{path} - Reverse-proxy HTTP to a process's exposed port
  (see docs/NETWORK-INGRESS-DESIGN.md)
- POST /configure - Reconfigure for new session (warm pool)
- POST /flush - Flush all pending synced files to storage
- GET /health - Health check

Storage backends supported:
- S3 (AWS S3 or MinIO)
- Azure Blob Storage

The sidecar has root access to shared volumes while the sandbox runs as non-root.
File operations (view, file-create, str-replace) read/write directly to shared volumes.
Only bash_tool needs process execution via nsenter into sandbox PID namespace.
"""

import sys

# Launched as `python main.py`, so this module runs as `__main__` -- but
# every sidecar_*.py sibling does `import main` to read shared config off it
# at call time. Without this alias, that `import main` doesn't find this
# already-running module under that name and instead re-executes this whole
# file from scratch as a second, separate "main" module, mid-way through the
# first execution's own sidecar_paths import -- a partially-initialized
# sidecar_paths.py then gets `import main`'d into existence again, and
# whichever name isn't defined yet in that partial module raises ImportError.
sys.modules.setdefault("main", sys.modules[__name__])

import asyncio
import base64
import ctypes
import ctypes.util
import hashlib
import hmac
import io
import json
import logging
import mimetypes
import os
import pty
import re
import select
import shlex
import shutil
import signal
import stat
import struct
import subprocess
import time as _time
from datetime import datetime
from fnmatch import fnmatch
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

import aiofiles
import httpx
from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, Field

try:
    from PIL import Image

    PILLOW_AVAILABLE = True
except ImportError:
    Image = None
    PILLOW_AVAILABLE = False

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    stream=sys.stdout
)
logger = logging.getLogger("sidecar")

# Ensure common file extensions resolve consistently even on minimal images.
mimetypes.add_type("text/markdown", ".md")
mimetypes.add_type("application/yaml", ".yaml")
mimetypes.add_type("application/yaml", ".yml")
mimetypes.add_type("application/x-ipynb+json", ".ipynb")


def _detect_content_type(local_path: str) -> str:
    """Best-effort MIME detection for storage metadata."""
    guessed_type, _ = mimetypes.guess_type(local_path)
    if guessed_type:
        return guessed_type

    # Fallback: classify unknown files as text/plain when they look like UTF-8 text.
    try:
        with open(local_path, "rb") as f:
            sample = f.read(8192)
    except Exception:
        return "application/octet-stream"

    if not sample:
        return "text/plain"
    if b"\x00" in sample:
        return "application/octet-stream"

    try:
        sample.decode("utf-8")
        return "text/plain"
    except UnicodeDecodeError:
        return "application/octet-stream"

# ============================================================================
# Configuration from environment
# ============================================================================

RUNTIME_MODE = os.environ.get("RUNTIME_MODE", "k8s")  # k8s or compose

# Shared volume paths (generic names, not agent-specific)
UPLOADS_DIR = "/mnt/user-data/uploads"
OUTPUTS_DIR = "/mnt/user-data/outputs"
SKILLS_DIR = "/mnt/skills"
WORKSPACE_DIR = "/workspace"
TMP_DIR = "/tmp"  # Ephemeral scratch space - NOT synced to storage

# Storage backend configuration
STORAGE_BACKEND = os.environ.get("STORAGE_BACKEND", "s3")  # s3, azure, or gcs

# S3 configuration (AWS S3 or MinIO)
S3_ENDPOINT = os.environ.get("STORAGE_S3_ENDPOINT") or os.environ.get("S3_ENDPOINT", None)


def _get_s3_bucket() -> str:
    return os.environ.get("STORAGE_S3_BUCKET") or os.environ.get("S3_BUCKET", "boxkite-sandbox")


S3_BUCKET = _get_s3_bucket()
AWS_REGION = os.environ.get("STORAGE_S3_REGION") or os.environ.get("AWS_REGION", "us-east-1")
AWS_ACCESS_KEY_ID = os.environ.get("AWS_ACCESS_KEY_ID", "")
AWS_SECRET_ACCESS_KEY = os.environ.get("AWS_SECRET_ACCESS_KEY", "")
AWS_SESSION_TOKEN = os.environ.get("AWS_SESSION_TOKEN", "")
S3_KMS_KEY_ID = os.environ.get("STORAGE_S3_KMS_KEY_ID") or os.environ.get("S3_KMS_KEY_ID", "")

# Azure Blob Storage configuration
# Check both env var names for compatibility (STORAGE_AZURE_* is used by backend/workers)
AZURE_STORAGE_CONNECTION_STRING = os.environ.get("STORAGE_AZURE_CONNECTION_STRING") or os.environ.get("AZURE_STORAGE_CONNECTION_STRING", "")
AZURE_STORAGE_ACCOUNT_NAME = os.environ.get("STORAGE_AZURE_ACCOUNT_NAME") or os.environ.get("AZURE_STORAGE_ACCOUNT_NAME", "")
AZURE_STORAGE_ACCOUNT_KEY = os.environ.get("STORAGE_AZURE_ACCOUNT_KEY") or os.environ.get("AZURE_STORAGE_ACCOUNT_KEY", "")
AZURE_STORAGE_SAS_TOKEN = os.environ.get("STORAGE_AZURE_SAS_TOKEN") or os.environ.get("AZURE_STORAGE_SAS_TOKEN", "")
AZURE_STORAGE_ACCOUNT_URL = (
    os.environ.get("STORAGE_AZURE_ACCOUNT_URL")
    or os.environ.get("AZURE_STORAGE_ACCOUNT_URL")
    or os.environ.get("STORAGE_AZURE_BLOB_ENDPOINT")
    or os.environ.get("AZURE_STORAGE_BLOB_ENDPOINT")
    or ""
)
AZURE_STORAGE_AUTH_MODE = (
    os.environ.get("STORAGE_AZURE_AUTH_MODE")
    or os.environ.get("AZURE_STORAGE_AUTH_MODE")
    or "auto"
).strip().lower().replace("-", "_")
AZURE_STORAGE_CLIENT_ID = os.environ.get("STORAGE_AZURE_CLIENT_ID") or os.environ.get("AZURE_CLIENT_ID", "")
AZURE_STORAGE_CONTAINER = os.environ.get("STORAGE_AZURE_CONTAINER") or os.environ.get("AZURE_STORAGE_CONTAINER", "boxkite-storage")

# Google Cloud Storage configuration. Auth defaults to Application Default
# Credentials (covers GKE Workload Identity with no extra config) -- unlike
# S3/Azure there is no key/connection-string fan-out to plumb here.
GCS_BUCKET = os.environ.get("STORAGE_GCS_BUCKET") or os.environ.get("GCS_BUCKET", "")
GCS_PROJECT = os.environ.get("STORAGE_GCS_PROJECT") or os.environ.get("GCS_PROJECT", "")

# Sandbox user UID/GID (must match sandbox container)
SANDBOX_UID = int(os.environ.get("SANDBOX_UID", "1001"))
SANDBOX_GID = int(os.environ.get("SANDBOX_GID", "1001"))


def _env_flag(name: str, default: str) -> bool:
    return os.environ.get(name, default).strip().lower() in {"1", "true", "yes", "on"}


SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED = _env_flag(
    "SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED",
    "true",
)

# docs/AGENT-PTY-DESIGN.md: an agent-callable true pseudo-terminal
# (POST /pty-exec), distinct from the human-operator-only WS /pty takeover
# below. New attack surface (a second PTY-allocation path, now reachable
# from agent-visible tool calls, not just an operator's own WS connection)
# -- off by default, same "flagged off until a security review" posture as
# BOXKITE_IMAGE_BUILDER_ENABLED.
BOXKITE_AGENT_PTY_ENABLED = _env_flag("BOXKITE_AGENT_PTY_ENABLED", "false")

# docs/NODE-INTERPRETER-DESIGN.md: a persistent, kept-alive Node.js process
# per session, the Node.js counterpart to the Python /interpreter/* routes
# below. New attack surface (a second kept-alive-interpreter code path,
# distinct from the already-reviewed Python one) -- off by default, same
# "flagged off until a security review" posture as BOXKITE_AGENT_PTY_ENABLED.
BOXKITE_NODE_INTERPRETER_ENABLED = _env_flag("BOXKITE_NODE_INTERPRETER_ENABLED", "false")

# docs/LSP-SUPPORT-SCOPING.md, GitHub issue #183: agent-invokable
# textDocument/completion (plus the handshake methods to get there) against
# two persistent language servers (pyright for Python,
# typescript-language-server for TypeScript/JS). New attack surface (a
# second family of kept-alive, agent-reachable subprocesses) -- off by
# default, same "flagged off until a security review" posture as
# BOXKITE_AGENT_PTY_ENABLED/BOXKITE_NODE_INTERPRETER_ENABLED.
BOXKITE_LSP_ENABLED = _env_flag("BOXKITE_LSP_ENABLED", "false")

# docs/BROWSER-EXEC-DESIGN.md: headless Chromium (Playwright/CDP) driven by
# one lazily-started, kept-alive browser process per session
# (/browser/navigate, /browser/exec, /browser/screenshot, /browser/close --
# GitHub issue #119). Off by default, same "flagged off until a security
# review" posture as BOXKITE_AGENT_PTY_ENABLED/BOXKITE_NODE_INTERPRETER_ENABLED
# -- but this one deserves MORE scrutiny than either before ever being turned
# on for a real multi-tenant deployment (design doc §5): unlike every other
# egress-needing tool in this file, the browser process resolves DNS and
# opens its own sockets directly, with no sidecar-side per-request
# destination check in the path (Chromium *is* the HTTP client here, not a
# caller going through one this file already instruments). See
# docs/BROWSER-EXEC-DESIGN.md §3 for the NetworkPolicy shape this requires
# and src/boxkite/browser_network_policy.py for its implementation.
BOXKITE_BROWSER_ENABLED = _env_flag("BOXKITE_BROWSER_ENABLED", "false")

# docs/GUI-COMPUTER-USE-SCOPING.md: human-facing GUI/remote-desktop takeover
# (WS /desktop -- Xvfb + a window manager + x11vnc, GitHub issue #184). Off
# by default, same "flagged off until a security review" posture as
# BOXKITE_AGENT_PTY_ENABLED/BOXKITE_BROWSER_ENABLED. This is the human-
# takeover slice only -- agent-programmatic GUI tool calls are explicitly
# out of scope, see the scoping doc's own "Deliberately out of scope"
# section.
BOXKITE_DESKTOP_ENABLED = _env_flag("BOXKITE_DESKTOP_ENABLED", "false")

# docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md: read-only S3 FUSE bucket
# mounting. Off by default, and even when true the /mount-bucket route
# below still fails closed with a clear 501 unless /dev/fuse is actually
# present in this container -- see build_s3fs_mount_command's own
# docstring for exactly why granting that device access is a separate,
# not-yet-made decision this flag alone does not make.
BOXKITE_FUSE_MOUNT_ENABLED = _env_flag("BOXKITE_FUSE_MOUNT_ENABLED", "false")

# ---------------------------------------------------------------------------
# Sidecar HTTP API authentication (defense in depth on top of NetworkPolicy).
#
# The sidecar has no authentication of its own beyond whatever network
# isolation the deployment provides, and that isolation is not guaranteed:
# NetworkPolicy enforcement is CNI-dependent, and even where enforced, a
# broad egress rule on the pod (e.g. allow-all-443 for storage) also governs
# what can reach this container's own ingress port, since both containers in
# a pod share one network namespace.
#
# SIDECAR_AUTH_TOKEN is generated fresh per-pod at pod-creation time by
# SandboxManager/WarmPoolManager (src/boxkite/sidecar_auth.py) — never a
# static, repo-wide secret — and injected here as an env var. The manager
# sends it back on every request via the SIDECAR_AUTH_HEADER header.
#
# These two names must stay in sync with src/boxkite/sidecar_auth.py; see
# tests/test_sidecar_auth_parity.py.
# ---------------------------------------------------------------------------
SIDECAR_AUTH_TOKEN_ENV = "SIDECAR_AUTH_TOKEN"
SIDECAR_AUTH_HEADER = "X-Sidecar-Auth-Token"
# deploy/pod-template.yaml's literal placeholder value -- a self-hoster who
# copies that template verbatim (skipping the comment above it) would
# otherwise get a plausible-looking token that "just works" as a shared,
# guessable secret across every pod. Must match
# src/boxkite/sidecar_auth.py's SIDECAR_AUTH_TOKEN_TEMPLATE_PLACEHOLDER (see
# tests/test_sidecar_auth_parity.py) and deploy/pod-template.yaml's literal
# value (see tests/test_pod_template_parity.py).
SIDECAR_AUTH_TOKEN_TEMPLATE_PLACEHOLDER = "CHANGEME-generate-a-random-per-pod-secret-see-comment-above"


def _normalize_sidecar_auth_token(raw: str) -> str:
    """Treat the template placeholder identically to an unset token, so the
    fail-closed 503 in enforce_sidecar_auth() below catches a self-hoster who
    copied deploy/pod-template.yaml verbatim, not just a truly empty value."""
    stripped = raw.strip()
    return "" if stripped == SIDECAR_AUTH_TOKEN_TEMPLATE_PLACEHOLDER else stripped


SIDECAR_AUTH_TOKEN = _normalize_sidecar_auth_token(os.environ.get(SIDECAR_AUTH_TOKEN_ENV, ""))

# ---------------------------------------------------------------------------
# Manager-to-sidecar TLS (see src/boxkite/tls.py,
# docs/SIDECAR-TRANSPORT-TLS-DESIGN.md). Same intentional duplication
# pattern as SIDECAR_AUTH_TOKEN_ENV/SIDECAR_AUTH_HEADER above: this process
# doesn't depend on the `boxkite` package, so these names/paths are
# re-declared as local constants rather than imported. Must stay in sync
# with src/boxkite/tls.py -- see tests/test_sidecar_tls_parity.py.
#
# SandboxManager/WarmPoolManager generate a fresh, short-lived, self-signed
# cert/key per pod at pod-creation time and mount it into this container at
# SIDECAR_TLS_MOUNT_PATH via a Secret volume (not secretKeyRef -- uvicorn's
# ssl_certfile/ssl_keyfile need filesystem paths). When both files are
# present, this process serves HTTPS; SIDECAR_TLS_DISABLED=true forces
# plain HTTP even if the files happen to be present (e.g. a stale mount
# left over from a recycled pod).
# ---------------------------------------------------------------------------
SIDECAR_TLS_DISABLED_ENV = "SIDECAR_TLS_DISABLED"
SIDECAR_TLS_MOUNT_PATH = "/etc/boxkite/tls"
SIDECAR_TLS_CERT_FILENAME = "tls.crt"
SIDECAR_TLS_KEY_FILENAME = "tls.key"

SIDECAR_TLS_DISABLED = os.environ.get(SIDECAR_TLS_DISABLED_ENV, "").strip().lower() == "true"
SIDECAR_TLS_CERT_PATH = os.path.join(SIDECAR_TLS_MOUNT_PATH, SIDECAR_TLS_CERT_FILENAME)
SIDECAR_TLS_KEY_PATH = os.path.join(SIDECAR_TLS_MOUNT_PATH, SIDECAR_TLS_KEY_FILENAME)


def _sidecar_tls_files_present() -> bool:
    """Whether this pod actually has a mounted cert/key to serve HTTPS with.

    Checked at call time (not cached at import time) so tests can drop
    files into a monkeypatched SIDECAR_TLS_MOUNT_PATH without reimporting
    this module -- matches the rest of this file's style of reading runtime
    state lazily rather than freezing it at import.
    """
    if SIDECAR_TLS_DISABLED:
        return False
    return os.path.isfile(SIDECAR_TLS_CERT_PATH) and os.path.isfile(SIDECAR_TLS_KEY_PATH)


# Routes that must remain reachable without the shared secret:
# - /health is polled by the K8s kubelet's liveness/readiness probes (plain
#   httpGet, no custom headers) and by the warm-pool reaper's idle check.
#   Its response body is low-sensitivity (session id, idle seconds, runtime
#   mode) — not a tool-execution or file-access capability.
_AUTH_EXEMPT_PATHS = {"/health"}

# Image payload optimization for model-bound read-image responses.
# Keeps visual quality while reducing base64/context size.
READ_IMAGE_MAX_DIMENSION = max(256, int(os.environ.get("SIDECAR_READ_IMAGE_MAX_DIM", "2048")))
READ_IMAGE_JPEG_QUALITY = max(30, min(95, int(os.environ.get("SIDECAR_READ_IMAGE_JPEG_QUALITY", "85"))))
READ_IMAGE_WEBP_QUALITY = max(30, min(95, int(os.environ.get("SIDECAR_READ_IMAGE_WEBP_QUALITY", "80"))))
READ_IMAGE_PNG_COMPRESS_LEVEL = max(0, min(9, int(os.environ.get("SIDECAR_READ_IMAGE_PNG_COMPRESS_LEVEL", "6"))))

# /grep runs an attacker-influenced regex (the pattern) against
# attacker-influenced content (anything the agent has written under its own
# roots) -- a catastrophic-backtracking pattern matched against a crafted
# line can otherwise pin a thread indefinitely. Python's stdlib `re` has no
# execution-timeout primitive, so this can't be fully closed without a
# timeout-capable regex engine (a real fix, tracked as a follow-up, not
# implemented here to avoid adding a new dependency in this pass) -- but
# offloading to a worker thread (see grep_files) at least keeps a stuck
# match from freezing the event loop / other requests / the K8s health
# probe, and this wall-clock budget bounds the non-pathological case (a
# huge tree with a benign-but-slow pattern) from running unbounded.
GREP_TIMEOUT_SECONDS = max(1, int(os.environ.get("SIDECAR_GREP_TIMEOUT_SECONDS", "10")))
GREP_MAX_BYTES_SCANNED = max(1_000_000, int(os.environ.get("SIDECAR_GREP_MAX_BYTES_SCANNED", "104857600")))

# ---------------------------------------------------------------------------
# Background process registry (/process/*) — see docs/PROCESS-SESSIONS-DESIGN.md.
#
# A background process is a standing resource commitment in a way a bounded
# /exec call never was, so it gets its own explicit caps rather than
# inheriting /exec's per-call timeout:
#   - SANDBOX_MAX_BACKGROUND_PROCESSES: per-session concurrent-process cap,
#     enforced in /process/start (429 once at the cap).
#   - SANDBOX_PROCESS_OUTPUT_MAX_BYTES: bounded ring buffer per process —
#     unlike EXEC_MAX_STDOUT_BYTES (which kills the process once hit), this
#     drops the *oldest* bytes to stay under the cap and reports
#     `truncated: true` to callers who ask for data that's already been
#     dropped, so a long-lived chatty process can't grow memory unbounded.
#   - SANDBOX_PROCESS_MAX_RUNTIME_SECONDS_CEILING: a hard, server-enforced
#     ceiling on the caller-supplied `max_runtime_seconds` — background
#     processes cannot be unbounded.
#   - SANDBOX_PROCESS_STOP_GRACE_PERIOD_SECONDS: SIGTERM, wait this long,
#     then SIGKILL if still alive.
SANDBOX_MAX_BACKGROUND_PROCESSES = max(1, int(os.environ.get("SANDBOX_MAX_BACKGROUND_PROCESSES", "4")))
PROCESS_OUTPUT_MAX_BYTES = max(4096, int(os.environ.get("SANDBOX_PROCESS_OUTPUT_MAX_BYTES", str(256 * 1024))))
PROCESS_MAX_RUNTIME_SECONDS_CEILING = max(
    60, int(os.environ.get("SANDBOX_PROCESS_MAX_RUNTIME_SECONDS_CEILING", str(4 * 3600)))
)
PROCESS_STOP_GRACE_PERIOD_SECONDS = max(1, int(os.environ.get("SANDBOX_PROCESS_STOP_GRACE_PERIOD_SECONDS", "5")))

# ---------------------------------------------------------------------------
# Per-session cumulative exec budget (GitHub issue #122).
#
# Every existing ceiling in this file bounds a single call or a single
# process: ExecRequest.timeout bounds one /exec call, max_runtime_seconds
# bounds one background process. None of them bound the *session* -- an
# agent stuck retrying the same failing command forever runs forever, each
# individual call finishing well inside its own timeout. These two
# ceilings apply across the whole session's lifetime instead, independent
# of any single call's own timeout:
#   - SANDBOX_SESSION_MAX_EXEC_COUNT: total exec-like calls this session
#     may make -- one unit per /exec call, per /interpreter/exec call,
#     per /node-interpreter/exec call, per /browser/navigate or
#     /browser/exec call, per /lsp/start call, per /lsp/{id}/completion
#     call, and per /process/start call (a background process counts once
#     at start time, not per poll).
#   - SANDBOX_SESSION_MAX_EXEC_SECONDS: cumulative wall-clock time spent
#     actually running code -- summed across /exec's and
#     /interpreter/exec's (/node-interpreter/exec's, /browser/navigate's,
#     /browser/exec's, /lsp/start's, /lsp/{id}/completion's) own measured
#     call durations. /process/start does NOT contribute to this total:
#     it returns before its spawned process finishes, so there is no
#     synchronous "call duration" to measure the way there is for the
#     other three routes -- see _reserve_session_exec_slot_or_raise's own
#     docstring. A background process still consumes one exec-count unit
#     per start, which is what actually stops "spin up unbounded
#     background processes" as a bypass.
# 0 disables the respective check. Both reset on every /configure (see
# _reset_session_exec_budget, called from sidecar_sync.configure) so a
# recycled pod starts a new tenant's session with a fresh budget.
#
# Coverage (security review found the first pass of this feature wired
# into /exec only -- see the note further down, right above
# `_session_budget_lock`, for the full history): the budget is now
# enforced, via the SAME counters and the SAME sticky
# `_session_budget_exceeded` flag, in every exec-like route: /exec
# (sidecar_execution.py), /interpreter/exec (sidecar_interpreter.py),
# /process/start (sidecar_processes.py), /node-interpreter/exec
# (sidecar_node_interpreter.py, opt-in), /pty-exec (sidecar_pty.py,
# opt-in), and /lsp/start + /lsp/{id}/completion (sidecar_lsp.py, opt-in,
# GitHub issue #183) -- a new exec-capable route added after issue #122's
# fix is wired into this shared mechanism from the start, not bolted on
# after a second security review finds the same gap again.
#
# On breach: _teardown_session_for_budget_breach kills every live
# background process/interpreter this session may have started (the same
# cleanup /configure performs before wiping state for the next tenant),
# and the breaching call gets a structured 403 body
# (_session_exec_budget_error_detail) instead of its normal response --
# deliberately a different HTTP status and shape than a plain timeout
# (200, exit_code=124) so a caller can tell them apart without
# string-matching stderr. `_session_budget_exceeded` is sticky: once set
# by a breach on ANY of the routes above, every subsequent call to ANY of
# them on this pod is rejected the same way until the next /configure
# claims it for a new session.
SANDBOX_SESSION_MAX_EXEC_COUNT = max(0, int(os.environ.get("SANDBOX_SESSION_MAX_EXEC_COUNT", "500")))
SANDBOX_SESSION_MAX_EXEC_SECONDS = max(
    0.0, float(os.environ.get("SANDBOX_SESSION_MAX_EXEC_SECONDS", "3600"))
)

_session_exec_count = 0
_session_exec_seconds = 0.0
_session_budget_exceeded: Optional[dict] = None


def _reset_session_exec_budget() -> None:
    """Start a fresh session's exec budget. Called from /configure."""
    global _session_exec_count, _session_exec_seconds, _session_budget_exceeded
    _session_exec_count = 0
    _session_exec_seconds = 0.0
    _session_budget_exceeded = None


def _session_exec_count_breach() -> Optional[dict]:
    """Whether this session is already at its exec-count ceiling -- checked
    BEFORE running the next command, since (unlike duration) the count is
    known upfront: a session at its ceiling never gets to run one more."""
    if SANDBOX_SESSION_MAX_EXEC_COUNT > 0 and _session_exec_count >= SANDBOX_SESSION_MAX_EXEC_COUNT:
        return {
            "reason": "exec_count",
            "limit": SANDBOX_SESSION_MAX_EXEC_COUNT,
            "used": _session_exec_count,
        }
    return None


def _session_exec_seconds_breach() -> Optional[dict]:
    """Whether this call's own duration just pushed cumulative exec time
    over the ceiling -- only knowable after the command has actually run."""
    if SANDBOX_SESSION_MAX_EXEC_SECONDS > 0 and _session_exec_seconds > SANDBOX_SESSION_MAX_EXEC_SECONDS:
        return {
            "reason": "exec_seconds",
            "limit": SANDBOX_SESSION_MAX_EXEC_SECONDS,
            "used": round(_session_exec_seconds, 3),
        }
    return None


def _session_exec_budget_error_detail(breach: dict) -> dict:
    """Structured error body /exec returns on a budget breach -- deliberately
    NOT shaped like ExecResponse (no exit_code/stdout/stderr) so a caller can
    tell a budget breach apart from a normal command timeout (200,
    exit_code=124) by HTTP status and body shape alone."""
    return {
        "error_type": "session_budget_exceeded",
        "reason": breach["reason"],
        "limit": breach["limit"],
        "used": breach["used"],
        "message": (
            f"Session exec budget exceeded ({breach['reason']}): "
            f"used={breach['used']} > limit={breach['limit']}. "
            "Session terminated; start a new session to continue."
        ),
    }


async def _teardown_session_for_budget_breach() -> None:
    """Kill every live resource this session's exec/interpreter calls may
    have started -- the same cleanup /configure performs before wiping
    filesystem state for the next tenant, run here because this session's
    budget is exhausted and it can no longer be trusted to keep running,
    not because a new tenant is about to claim this pod."""
    await _kill_all_processes()
    await _reset_interpreter()
    await _reset_node_interpreter()
    await _reset_browser()
    await _kill_all_lsp_servers()


# Coverage note (security review on GitHub issue #122's first pass): the
# budget above was originally wired into /exec only. A session stuck in a
# retry loop that happened to loop via the persistent Python interpreter
# (/interpreter/exec, sidecar_interpreter.py -- default-enabled, same as
# bash_tool) or via background processes (/process/start,
# sidecar_processes.py -- also default-enabled) spent zero budget and was
# never throttled; worse, a session that had ALREADY tripped the sticky
# `_session_budget_exceeded` flag via /exec could immediately start a new
# interpreter call or background process completely unobstructed, since
# neither route read that flag. `_reserve_session_exec_slot_or_raise` and
# `_record_session_exec_duration_or_raise` below are the shared entry
# points every exec-like route now goes through -- /exec
# (sidecar_execution.py), /interpreter/exec (sidecar_interpreter.py),
# /node-interpreter/exec (sidecar_node_interpreter.py, opt-in),
# /process/start (sidecar_processes.py), /pty-exec (sidecar_pty.py,
# opt-in), and /browser/navigate + /browser/exec (sidecar_browser.py,
# opt-in) -- so there is exactly one budget, not a parallel counter per
# route.
_session_budget_lock: Optional[asyncio.Lock] = None


def _get_session_budget_lock() -> asyncio.Lock:
    """Lazily create the lock in the active event loop (same pattern as
    _get_flush_lock/_get_interpreter_lock/_get_process_registry_lock).
    Guards every read-and-mutate of `_session_exec_count`/
    `_session_exec_seconds`/`_session_budget_exceeded` across all callers."""
    global _session_budget_lock
    if _session_budget_lock is None:
        _session_budget_lock = asyncio.Lock()
    return _session_budget_lock


async def _reserve_session_exec_slot_or_raise(source: str = "session_budget") -> None:
    """Fail-closed entry point every exec-like route must call FIRST, before
    doing any actual work (running a command, one interpreter call,
    spawning a background process).

    Folds the exec-count ceiling check and its increment into one
    lock-guarded step. This closes a TOCTOU a security review flagged
    against the original single-route (/exec only) implementation: the
    count-ceiling check and the corresponding increment used to be
    separated by an unguarded `await` (the command's own execution), so N
    concurrent calls near the ceiling could all pass the check before any
    of them recorded its usage, overshooting the ceiling by up to N-1
    calls. Unlike exec-seconds (only knowable after a call finishes
    running -- see `_record_session_exec_duration_or_raise` below), the
    count doesn't depend on the call's outcome or duration at all, so
    there's no reason to defer incrementing it until after the work
    completes, and every reason not to -- deferring it is exactly what
    reopens this race.

    Raises HTTPException(403) with the same structured
    `session_budget_exceeded` body every route already used, either
    because the session is already sticky-exceeded (from a breach via any
    route) or because this call would be the one to cross the count
    ceiling -- in the latter case this also sets the sticky flag and tears
    the session down.
    """
    global _session_budget_exceeded, _session_exec_count
    async with _get_session_budget_lock():
        if _session_budget_exceeded is not None:
            raise HTTPException(
                status_code=403,
                detail=_session_exec_budget_error_detail(_session_budget_exceeded),
            )

        count_breach = _session_exec_count_breach()
        if count_breach is not None:
            _session_budget_exceeded = count_breach
            await _teardown_session_for_budget_breach()
            logger.warning(f"[{source}] session exec budget exceeded: {count_breach}; session torn down")
            raise HTTPException(status_code=403, detail=_session_exec_budget_error_detail(count_breach))

        _session_exec_count += 1


async def _record_session_exec_duration_or_raise(duration_seconds: float, source: str = "session_budget") -> None:
    """Record a completed call's duration against the session's cumulative
    exec-seconds total, called AFTER the actual work finishes (duration is
    only knowable then, unlike the count -- see
    `_reserve_session_exec_slot_or_raise` above, which every caller of this
    function must have already called before doing its work).

    Raises HTTPException(403) (and tears the session down) if this pushed
    cumulative exec-seconds over the ceiling -- the breaching call's own
    result is discarded in favor of the structured budget-exceeded body,
    same as the original /exec-only implementation.
    """
    global _session_budget_exceeded, _session_exec_seconds
    async with _get_session_budget_lock():
        _session_exec_seconds += max(0.0, duration_seconds)
        seconds_breach = _session_exec_seconds_breach()
        if seconds_breach is not None:
            _session_budget_exceeded = seconds_breach
            await _teardown_session_for_budget_breach()
            logger.warning(f"[{source}] session exec budget exceeded: {seconds_breach}; session torn down")
            raise HTTPException(status_code=403, detail=_session_exec_budget_error_detail(seconds_breach))


# Sidecar-restart / orphan-process survival (docs/PROCESS-SESSIONS-DESIGN.md
# section 2(b), SECURITY.md's "not yet verified" entry -- both now resolved
# by a real, tested experiment; see the design doc for the write-up).
#
# `/process/start`'s K8s-mode spawn wraps the real command in
# `nsenter -t <pid> -m -p ...` (build_k8s_exec_command). nsenter forks
# internally to actually enter the target PID namespace, so the
# `asyncio.subprocess.Process` handle the sidecar tracks (nsenter itself) is
# NOT the same OS process as the sandboxed command actually running --
# verified directly: killing only the tracked PID leaves the real command
# alive and running. Two independent fixes close this:
#   - BACKGROUND_PROCESS_MARKER_ENV: injected only into K8s-mode
#     /process/start spawns (never /exec, never the interpreter, never
#     pty) so a freshly-started sidecar can recognize a leftover from a
#     PREVIOUS incarnation via /proc/<pid>/environ -- immune to tampering by
#     the sandboxed process itself adding a *different* process's marker,
#     since each process's own /proc/<pid>/environ reflects only its own
#     exec-time environment.
#   - SANDBOX_PROCESS_STARTUP_SWEEP_ENABLED: gates the startup-time scan
#     that finds and kills those survivors (see
#     _sweep_orphaned_background_processes). Default on -- this is a bug
#     fix using capabilities the sidecar already holds (root, CAP_SYS_PTRACE
#     in K8s mode), not new attack surface, but stays configurable in case
#     an operator's environment makes the /proc scan itself undesirable.
BACKGROUND_PROCESS_MARKER_ENV = "_BOXKITE_BACKGROUND_PROCESS"
BACKGROUND_PROCESS_MARKER_VALUE = "1"
SANDBOX_PROCESS_STARTUP_SWEEP_ENABLED = os.environ.get(
    "SANDBOX_PROCESS_STARTUP_SWEEP_ENABLED", "true"
).strip().lower() not in ("false", "0", "no")

# ---------------------------------------------------------------------------
# Network ingress preview (/preview/{port}/...) — see
# docs/NETWORK-INGRESS-DESIGN.md. A background process may opt in (via
# ProcessStartRequest.expose_port) to being reachable from the sidecar's own
# loopback for HTTP proxying. This is bounded to a plausible user-service
# port range (never the sidecar's own port, never a privileged port) and to
# ports a live tracked process has actually registered -- /preview never
# proxies to an arbitrary, unregistered port.
PREVIEW_PORT_MIN = max(1, int(os.environ.get("SANDBOX_PREVIEW_PORT_MIN", "1024")))
PREVIEW_PORT_MAX = min(65535, int(os.environ.get("SANDBOX_PREVIEW_PORT_MAX", "65535")))
SIDECAR_PORT = int(os.environ.get("SIDECAR_PORT", "8080"))
# Preview responses are now TRUE streamed (see preview_proxy in
# sidecar_processes.py) rather than fully buffered before being returned --
# the response body is forwarded to the caller chunk by chunk as it arrives
# from the upstream dev server, so the sidecar never holds more than one
# chunk in memory at a time regardless of total response size.
# SANDBOX_PREVIEW_MAX_RESPONSE_BYTES is now an OPTIONAL total-size safety
# valve, not the memory-pressure mitigation it used to be when responses
# were buffered -- unset (or <= 0) means no cap at all, which is the new
# default, since streaming already removes the original OOM motivation for
# a hard cap. An operator who wants a bandwidth/abuse ceiling on this
# specific path can still set one.
_raw_preview_max_response_bytes = int(os.environ.get("SANDBOX_PREVIEW_MAX_RESPONSE_BYTES", "0"))
PREVIEW_MAX_RESPONSE_BYTES = (
    0 if _raw_preview_max_response_bytes <= 0 else max(65536, _raw_preview_max_response_bytes)
)
PREVIEW_UPSTREAM_TIMEOUT_SECONDS = max(
    1.0, float(os.environ.get("SANDBOX_PREVIEW_UPSTREAM_TIMEOUT_SECONDS", "30"))
)
# Overall wall-clock ceiling on one streamed preview response/connection --
# distinct from PREVIEW_UPSTREAM_TIMEOUT_SECONDS, which only bounds the time
# between individual reads. Without this, a dev server that dribbles out an
# SSE/long-poll response one byte every few seconds forever could hold a
# sidecar-side httpx client and its connection pool slot open indefinitely.
# This is the streaming era's equivalent safety valve to the old byte cap.
PREVIEW_STREAM_MAX_SECONDS = max(
    1.0, float(os.environ.get("SANDBOX_PREVIEW_STREAM_MAX_SECONDS", "300"))
)
# Hop-by-hop headers (RFC 7230 §6.1) plus the sidecar's own auth header must
# never be forwarded in either direction across the proxy boundary.
_PREVIEW_HOP_BY_HOP_HEADERS = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "content-length", "host",
}
# Persistent Python interpreter (see /interpreter/exec below).
#
# Distinct from /exec: /exec always spawns a fresh `python3 -c ...` process
# per call, so variables never survive between tool calls. This gives an
# agent one kept-alive Python process per session -- the interpreter's
# globals dict persists across calls until it's reset, times out from
# inactivity, or the session is torn down/recycled.
#
# Resource accounting mirrors /exec's own EXEC_MAX_STDOUT_BYTES cap and
# SandboxManager's session idle-reap concept, applied to this one
# always-running process instead of a one-shot call:
#   - INTERPRETER_IDLE_TIMEOUT_SECONDS: the interpreter is killed (not the
#     whole sandbox session) after this long with no /interpreter/exec
#     calls, freeing its memory without affecting bash_tool/file tools.
#   - INTERPRETER_MAX_MEMORY_MB: enforced via `ulimit -v` on the interpreter
#     process itself (virtual memory address space cap), same spirit as a
#     cgroup memory limit but scoped to just this one process rather than
#     the whole sandbox container.
#   - INTERPRETER_MAX_OUTPUT_BYTES: caps a single call's captured stdout,
#     same rationale as EXEC_MAX_STDOUT_BYTES -- a chatty snippet (an
#     infinite print loop) must not grow the response, or the driver's
#     in-memory buffer, without bound.
# ---------------------------------------------------------------------------
INTERPRETER_IDLE_TIMEOUT_SECONDS = max(
    30, int(os.environ.get("INTERPRETER_IDLE_TIMEOUT_SECONDS", "900"))
)
INTERPRETER_MAX_MEMORY_MB = max(64, int(os.environ.get("INTERPRETER_MAX_MEMORY_MB", "1024")))
INTERPRETER_MAX_OUTPUT_BYTES = max(
    4096, int(os.environ.get("INTERPRETER_MAX_OUTPUT_BYTES", str(256 * 1024)))
)
INTERPRETER_STARTUP_TIMEOUT_SECONDS = max(
    1, int(os.environ.get("INTERPRETER_STARTUP_TIMEOUT_SECONDS", "15"))
)
INTERPRETER_MAX_EXEC_TIMEOUT_SECONDS = max(
    1, int(os.environ.get("INTERPRETER_MAX_EXEC_TIMEOUT_SECONDS", "300"))
)

# Persistent Node.js interpreter (see /node-interpreter/exec below and
# docs/NODE-INTERPRETER-DESIGN.md). Same accounting shape as the Python
# INTERPRETER_* constants above, tuned by an independent set of env vars so
# an operator can size the two kept-alive processes differently.
#
# NODE_INTERPRETER_MAX_MEMORY_MB is enforced via Node's own
# `--max-old-space-size` flag, NOT `ulimit -v` the way the Python
# interpreter's memory cap is -- V8 reserves a large virtual address space
# for its heap at startup regardless of how much it ends up using, so a
# `ulimit -v` anywhere near a reasonable memory budget reliably makes `node`
# fail to start at all (a well-known V8/Node gotcha, not specific to this
# codebase). `--max-old-space-size` bounds the old-generation heap instead,
# which is the correct, Node-idiomatic knob for this -- see
# docs/NODE-INTERPRETER-DESIGN.md's "Known limitations" section for the
# residual risk this doesn't cover (off-heap/native memory, e.g. Buffers).
NODE_INTERPRETER_IDLE_TIMEOUT_SECONDS = max(
    30, int(os.environ.get("NODE_INTERPRETER_IDLE_TIMEOUT_SECONDS", "900"))
)
NODE_INTERPRETER_MAX_MEMORY_MB = max(
    64, int(os.environ.get("NODE_INTERPRETER_MAX_MEMORY_MB", "1024"))
)
NODE_INTERPRETER_MAX_OUTPUT_BYTES = max(
    4096, int(os.environ.get("NODE_INTERPRETER_MAX_OUTPUT_BYTES", str(256 * 1024)))
)
NODE_INTERPRETER_STARTUP_TIMEOUT_SECONDS = max(
    1, int(os.environ.get("NODE_INTERPRETER_STARTUP_TIMEOUT_SECONDS", "15"))
)
NODE_INTERPRETER_MAX_EXEC_TIMEOUT_SECONDS = max(
    1, int(os.environ.get("NODE_INTERPRETER_MAX_EXEC_TIMEOUT_SECONDS", "300"))
)

# Persistent language servers (see /lsp/* below, sidecar_lsp.py, and
# docs/LSP-SUPPORT-SCOPING.md). Unlike the two interpreters above (one
# process per session, always the same language), a session may run up to
# LSP_MAX_SERVERS of these concurrently (e.g. one for Python, one for
# TypeScript) -- so this is a registry (main._lsp_registry), not a single
# handle slot.
#   - LSP_STARTUP_TIMEOUT_SECONDS: bounds the initialize handshake: spawn
#     the real language server binary, then wait for its response to the
#     first JSON-RPC request. Longer than the interpreters' own startup
#     timeout -- a real language server does real analysis work (indexing
#     typeshed stubs, loading the TS compiler) before it can answer.
#   - LSP_REQUEST_TIMEOUT_SECONDS: per-call ceiling for
#     /lsp/{id}/completion's textDocument/completion round-trip.
#   - LSP_SHUTDOWN_TIMEOUT_SECONDS: how long /lsp/{id}/stop waits for the
#     `shutdown` request's response before giving up on graceful shutdown.
#   - LSP_SHUTDOWN_GRACE_PERIOD_SECONDS: how long to wait for the process
#     to actually exit after the `exit` notification before a hard
#     process-group SIGKILL.
#   - LSP_IDLE_TIMEOUT_SECONDS: idle-reap threshold, same concept as
#     INTERPRETER_IDLE_TIMEOUT_SECONDS/NODE_INTERPRETER_IDLE_TIMEOUT_SECONDS.
#   - LSP_MAX_SERVERS: per-session concurrent-server cap (429 once at the
#     cap) -- a language server is a real resource commitment (memory, a
#     real subprocess), same reasoning as SANDBOX_MAX_BACKGROUND_PROCESSES.
LSP_STARTUP_TIMEOUT_SECONDS = max(1, int(os.environ.get("LSP_STARTUP_TIMEOUT_SECONDS", "20")))
LSP_REQUEST_TIMEOUT_SECONDS = max(1, int(os.environ.get("LSP_REQUEST_TIMEOUT_SECONDS", "30")))
LSP_SHUTDOWN_TIMEOUT_SECONDS = max(1, int(os.environ.get("LSP_SHUTDOWN_TIMEOUT_SECONDS", "5")))
LSP_SHUTDOWN_GRACE_PERIOD_SECONDS = max(
    1, int(os.environ.get("LSP_SHUTDOWN_GRACE_PERIOD_SECONDS", "5"))
)
LSP_IDLE_TIMEOUT_SECONDS = max(30, int(os.environ.get("LSP_IDLE_TIMEOUT_SECONDS", "900")))
LSP_MAX_SERVERS = max(1, int(os.environ.get("SANDBOX_LSP_MAX_SERVERS", "2")))

# Headless browser automation (see /browser/* below and
# docs/BROWSER-EXEC-DESIGN.md §4). Same "one kept-alive process per session,
# same three-knob accounting shape as the interpreters" convention as
# NODE_INTERPRETER_* above, tuned by its own independent env vars:
#   - BROWSER_IDLE_TIMEOUT_SECONDS: kills the browser process (not the whole
#     sandbox session) after this long with no /browser/* calls.
#   - BROWSER_MAX_MEMORY_MB: advisory ONLY -- a real Chromium process's
#     memory footprint isn't controlled by a single flag the way V8's
#     `--max-old-space-size` bounds the Node interpreter's heap (design
#     doc §4). This is not wired to any enforcement mechanism; it exists so
#     an operator has one documented knob to reason about when sizing the
#     sandbox pod (see the design doc's recommendation to require at least
#     the `medium` SandboxSizeSpec tier for browser-enabled sessions).
#   - BROWSER_STARTUP_TIMEOUT_SECONDS: launching a real browser is slower
#     than starting a bare `node`/`python3` interpreter process, so this
#     defaults higher than INTERPRETER_STARTUP_TIMEOUT_SECONDS/
#     NODE_INTERPRETER_STARTUP_TIMEOUT_SECONDS.
#   - BROWSER_MAX_EXEC_TIMEOUT_SECONDS: per-call ceiling for
#     /browser/navigate and /browser/exec (both cap the caller's own
#     requested timeout_seconds against this).
#   - BROWSER_MAX_SCREENSHOT_BYTES: caps a single /browser/screenshot's PNG
#     payload. Unlike NODE_INTERPRETER_MAX_OUTPUT_BYTES (text, safely
#     truncatable mid-stream), a truncated PNG is corrupt/undecodable --
#     the driver rejects an oversized screenshot with an error instead of
#     silently truncating it (see sidecar_browser.py).
BROWSER_IDLE_TIMEOUT_SECONDS = max(30, int(os.environ.get("BROWSER_IDLE_TIMEOUT_SECONDS", "600")))
BROWSER_MAX_MEMORY_MB = max(256, int(os.environ.get("BROWSER_MAX_MEMORY_MB", "1024")))
BROWSER_STARTUP_TIMEOUT_SECONDS = max(
    1, int(os.environ.get("BROWSER_STARTUP_TIMEOUT_SECONDS", "45"))
)
BROWSER_MAX_EXEC_TIMEOUT_SECONDS = max(
    1, int(os.environ.get("BROWSER_MAX_EXEC_TIMEOUT_SECONDS", "120"))
)
BROWSER_MAX_SCREENSHOT_BYTES = max(
    65536, int(os.environ.get("BROWSER_MAX_SCREENSHOT_BYTES", str(5 * 1024 * 1024)))
)

# ---------------------------------------------------------------------------
# Periodic sync: continuously persist workspace files to blob storage so that
# pod recovery can restore session state from storage rather than replaying
# every tool call.  Two cadences:
#   - flush (30s):     upload files already in the pending_sync_files set.
#   - reconcile (120s): walk the filesystem to discover untracked files (e.g.
#                        files created by bash commands that bypass sidecar API).
# See docs/dev-guides/sidecar-sync-architecture.md for the full design.
# ---------------------------------------------------------------------------
SYNC_FLUSH_INTERVAL_SEC = max(10, int(os.environ.get("SYNC_FLUSH_INTERVAL_SEC", "30")))
SYNC_RECONCILE_INTERVAL_SEC = max(
    SYNC_FLUSH_INTERVAL_SEC,
    int(os.environ.get("SYNC_RECONCILE_INTERVAL_SEC", "120")),
)
# Stable-file guard: wait this long and re-check file signature before upload
# to avoid syncing a half-written file (e.g. mid `pip install` or large write).
SYNC_STABLE_CHECK_INTERVAL_MS = max(0, int(os.environ.get("SYNC_STABLE_CHECK_INTERVAL_MS", "750")))
SYNC_SHUTDOWN_FLUSH_TIMEOUT_SEC = max(1, int(os.environ.get("SYNC_SHUTDOWN_FLUSH_TIMEOUT_SEC", "20")))
SYNC_IGNORE_DIRS_DEFAULT = {
    ".git",
    ".cache",
    ".config",
    ".next",
    "node_modules",
    "dist",
    "build",
    "target",
    "__pycache__",
}
SYNC_IGNORE_DIRS = {
    token.strip()
    for token in os.environ.get("SYNC_IGNORE_DIRS", ",".join(sorted(SYNC_IGNORE_DIRS_DEFAULT))).split(",")
    if token.strip()
}
# Cap the in-memory signature cache to prevent unbounded growth in long sessions.
SYNC_SIGNATURE_MAX_ENTRIES = max(1000, int(os.environ.get("SYNC_SIGNATURE_MAX_ENTRIES", "50000")))

# SECURITY: Safe environment for subprocess execution
# This is the ONLY environment passed to nsenter/docker-exec commands.
# DO NOT add any credentials, API keys, or sensitive data here.
# The sidecar has Azure/S3 credentials but they must NOT leak to agent code.
SAFE_EXEC_ENV = {
    "PATH": "/usr/local/bin:/usr/bin:/bin",
    "HOME": "/workspace",
    "LANG": "C.UTF-8",
    "LC_ALL": "C.UTF-8",
    "PYTHONUNBUFFERED": "1",
    "PYTHONDONTWRITEBYTECODE": "1",
    "TERM": "xterm",
    "NODE_PATH": "/usr/local/lib/node_modules",
    "PLAYWRIGHT_BROWSERS_PATH": "/ms-playwright",
    # Redirect XDG dirs to /tmp so programs like LibreOffice write their
    # config/cache there instead of $HOME/.config (which lives in /workspace
    # and gets synced to blob storage).  Root-owned prefetch of those dirs
    # on session restore caused LibreOffice to crash (WrappedTargetRuntimeException).
    "XDG_CONFIG_HOME": "/tmp/.config",
    "XDG_CACHE_HOME": "/tmp/.cache",
}

# SECURITY: ExecRequest.secret_env's env-var-NAME side had no validation at
# all -- an agent could map any key (including these) to a granted secret's
# value. The agent already has an arbitrary shell and could already set any
# of these itself via `export X=y; cmd`, so this isn't a net-new way to
# influence a spawned process's environment -- but it IS a net-new way to
# smuggle a secret's plaintext *value* into an interpreter-hijack vector
# (a library-preload or startup-script env var an interpreter treats as
# code/config to load, not just data) as a side effect of resolving a name
# the agent never sees the value of. Reject known-dangerous names outright;
# see `_validate_secret_env_var_name` below for the identifier-format check.
_SECRET_ENV_DENYLIST = frozenset({
    "PATH", "IFS", "ENV", "BASH_ENV", "BASH_FUNC",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    "DYLD_INSERT_LIBRARIES", "DYLD_LIBRARY_PATH", "DYLD_FRAMEWORK_PATH",
    "PYTHONSTARTUP", "PYTHONPATH", "PERL5OPT", "NODE_OPTIONS", "NODE_PATH",
})
_SECRET_ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _validate_secret_env_var_name(env_var_name: str) -> bool:
    """True if `env_var_name` is safe to use as a secret_env target: a
    well-formed POSIX environment-variable identifier (blocks embedded
    `=`/null bytes/empty strings reaching the subprocess env= dict) and not
    one of the interpreter-hijack vectors in `_SECRET_ENV_DENYLIST` above."""
    if not _SECRET_ENV_VAR_NAME_RE.match(env_var_name):
        return False
    return env_var_name.upper() not in _SECRET_ENV_DENYLIST

# Session state
current_session = {
    "session_id": None,
    "organization_id": None,
    "work_item_id": None,
    "storage_prefix": None,
    "skills_rev": None,
    "configured_at": None,
    # Secrets broker (docs/SECRETS-DESIGN.md §3/4) -- non-sensitive grant
    # metadata only. secret_allowed_hosts maps name -> allowlist.
    "secret_names": [],
    "secret_allowed_hosts": {},
    "secret_capability_token": None,
    "secrets_control_plane_url": None,
}

# In-memory, per-session TTL cache of resolved secret VALUES (never
# persisted, never logged) -- docs/SECRETS-DESIGN.md §4's recommended
# middle ground between "fetch every call" (safest, slowest) and "fetch
# once and hold for the session's whole lifetime" (fastest, largest blast
# radius). Cleared on every /configure (session start/recycle) so a
# recycled pod never serves a previous tenant's cached secret value.
_SECRET_VALUE_CACHE_TTL_SECONDS = 300
_secret_value_cache: dict[str, tuple[str, float]] = {}  # name -> (value, expires_at_monotonic)

# Track files pending storage sync (canonical virtual paths like "/workspace/foo.py").
# Populated by: file-create, str-replace, and _discover_untracked_sync_files().
# Drained by: flush_outputs() after successful upload.
pending_sync_files = set()
# Signature cache: virtual_path → (size_bytes, mtime_ns) after last successful upload.
# Used by _discover_untracked_sync_files() to skip unchanged files and avoid
# re-uploading the entire workspace every reconcile cycle.
synced_file_signatures: dict[str, tuple[int, int]] = {}

# Sync orchestration state.
# _flush_lock serializes: configure, /flush, present_files, periodic flush, and
# shutdown flush — so concurrent requests don't race over pending_sync_files.
_flush_lock: Optional[asyncio.Lock] = None
_last_reconcile_at: float = 0.0
_periodic_sync_task: Optional[asyncio.Task] = None

# Activity tracking for idle-based TTL.
# Updated on every sidecar request except /health (so the reaper's own
# health polls don't reset the idle timer).
_last_activity_at: float = _time.monotonic()

# Persistent Python interpreter state (see the INTERPRETER_* constants above
# and _InterpreterHandle below). One interpreter process per sidecar/session
# -- there's only ever one, never a dict keyed by an id, matching the scope
# in docs/DAYTONA-COMPARISON.md ("one Python process alive per session").
_interpreter_handle: Optional["_InterpreterHandle"] = None
_interpreter_lock: Optional[asyncio.Lock] = None

# Persistent Node.js interpreter state -- same one-per-session shape as the
# Python interpreter state above, see docs/NODE-INTERPRETER-DESIGN.md.
_node_interpreter_handle: Optional["_NodeInterpreterHandle"] = None
_node_interpreter_lock: Optional[asyncio.Lock] = None

# Persistent headless-browser (Playwright/Chromium) state -- same
# one-per-session shape, see docs/BROWSER-EXEC-DESIGN.md.
_browser_handle: Optional["_BrowserHandle"] = None
_browser_lock: Optional[asyncio.Lock] = None

# Persistent language-server state (see /lsp/* below, sidecar_lsp.py, and
# docs/LSP-SUPPORT-SCOPING.md) -- a REGISTRY, not a single handle slot,
# since a session may run more than one language server at once (e.g. one
# for Python, one for TypeScript), unlike the interpreters/browser above.
_lsp_registry: dict[str, "LspServerHandle"] = {}
_lsp_registry_lock: Optional[asyncio.Lock] = None

# FastAPI app
app = FastAPI(title="Sandbox Sidecar", version="1.0.0")


@app.middleware("http")
async def enforce_sidecar_auth(request, call_next):
    """
    Require a valid shared-secret header on every route except /health.

    Defense in depth on top of NetworkPolicy (see the SIDECAR_AUTH_TOKEN
    comment above for why network isolation alone is not sufficient).

    Fails CLOSED: if SIDECAR_AUTH_TOKEN is not configured, every protected
    route returns 503 rather than silently running unauthenticated. This is
    intentional — a self-hoster who hasn't wired the secret through should
    get a loud, obvious failure, not a quietly-open HTTP API.
    """
    if request.url.path in _AUTH_EXEMPT_PATHS:
        return await call_next(request)

    if not SIDECAR_AUTH_TOKEN:
        return JSONResponse(
            status_code=503,
            content={
                "detail": (
                    "Sidecar auth is not configured (SIDECAR_AUTH_TOKEN is unset). "
                    "Refusing to serve requests until a per-pod secret is provisioned. "
                    "See SECURITY.md."
                )
            },
        )

    supplied = request.headers.get(SIDECAR_AUTH_HEADER, "")
    if not supplied or not hmac.compare_digest(supplied, SIDECAR_AUTH_TOKEN):
        return JSONResponse(
            status_code=401,
            content={"detail": "Missing or invalid sidecar auth token"},
        )

    return await call_next(request)


@app.middleware("http")
async def track_activity(request, call_next):
    """Update last-activity timestamp on every request except /health."""
    global _last_activity_at
    if request.url.path != "/health":
        _last_activity_at = _time.monotonic()
    return await call_next(request)


# ============================================================================
# Metrics (docs/E2B-COMPARISON.md's "OpenTelemetry & Metrics" gap-table row)
# ============================================================================
#
# Deliberately NOT a full OpenTelemetry SDK integration: that would mean an
# OTLP exporter pushing spans/metrics to an external collector, which is a
# new *outbound* network path in a sidecar whose whole security model is
# default-deny egress (see SECURITY.md). Instead this is a pull-only,
# dependency-free Prometheus text-exposition endpoint -- a collector scrapes
# *in*, the sidecar never dials *out*. No new egress, no new dependency,
# same "additive observability, no new attack surface" bar every other
# change in this file is held to.
_METRICS_START_TIME = _time.monotonic()
_metrics_request_counts: dict[str, int] = {}
_metrics_request_errors: dict[str, int] = {}
_metrics_exec_count = 0
_metrics_exec_errors = 0


def _metrics_route_label(path: str) -> str:
    """Collapse path params (process ids, etc.) into a stable route label.

    Prevents unbounded cardinality growth in _metrics_request_counts --
    `/process/{id}/output` for 10,000 different process ids must collapse
    to one label, not 10,000.
    """
    parts = path.strip("/").split("/")
    collapsed = [
        "{id}" if part and part not in _METRICS_KNOWN_SEGMENTS else part
        for part in parts
    ]
    return "/" + "/".join(collapsed) if collapsed != [""] else "/"


_METRICS_KNOWN_SEGMENTS = {
    "health", "exec", "http-request", "process", "start", "output", "input",
    "stop", "kill-all", "interpreter", "reset", "status", "ensure-skills",
    "inject-skills", "file-create", "view", "read-image", "str-replace",
    "present-files", "ls", "glob", "grep", "configure", "prefetch-uploads",
    "flush", "confirmed", "tool-call", "metrics", "pty",
}


@app.middleware("http")
async def record_metrics(request, call_next):
    """Count requests per route and per outcome bucket (2xx/4xx/5xx).

    Purely additive: reads nothing sensitive, exposes only counts and a
    route label with path params collapsed out (see _metrics_route_label).
    """
    route = _metrics_route_label(request.url.path)
    response = await call_next(request)
    _metrics_request_counts[route] = _metrics_request_counts.get(route, 0) + 1
    if response.status_code >= 400:
        _metrics_request_errors[route] = _metrics_request_errors.get(route, 0) + 1
    return response


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus text-exposition endpoint. Same auth as every other route
    (not in _AUTH_EXEMPT_PATHS) -- request counts/timings are lower-sensitivity
    than file contents or command output, but still internal operational
    detail, not something to expose to an unauthenticated caller by default.
    """
    lines = [
        "# HELP boxkite_sidecar_uptime_seconds Seconds since this sidecar process started.",
        "# TYPE boxkite_sidecar_uptime_seconds gauge",
        f"boxkite_sidecar_uptime_seconds {_time.monotonic() - _METRICS_START_TIME:.3f}",
        "# HELP boxkite_sidecar_requests_total Requests handled, by route.",
        "# TYPE boxkite_sidecar_requests_total counter",
    ]
    for route, count in sorted(_metrics_request_counts.items()):
        lines.append(f'boxkite_sidecar_requests_total{{route="{route}"}} {count}')
    lines.append("# HELP boxkite_sidecar_request_errors_total Requests with a 4xx/5xx response, by route.")
    lines.append("# TYPE boxkite_sidecar_request_errors_total counter")
    for route, count in sorted(_metrics_request_errors.items()):
        lines.append(f'boxkite_sidecar_request_errors_total{{route="{route}"}} {count}')
    lines.append("# HELP boxkite_sidecar_exec_total Commands executed via /exec.")
    lines.append("# TYPE boxkite_sidecar_exec_total counter")
    lines.append(f"boxkite_sidecar_exec_total {_metrics_exec_count}")
    lines.append("# HELP boxkite_sidecar_exec_errors_total /exec calls that returned a non-zero exit code.")
    lines.append("# TYPE boxkite_sidecar_exec_errors_total counter")
    lines.append(f"boxkite_sidecar_exec_errors_total {_metrics_exec_errors}")
    return Response(content="\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


# ============================================================================
# Request/Response Models
# ============================================================================

class ExecRequest(BaseModel):
    command: str
    timeout: int = 30
    description: Optional[str] = None
    # SECURITY: still no arbitrary caller-supplied `env` field -- that
    # earlier removal (see docs/SECRETS-DESIGN.md's bash_tool addendum for
    # the full history) stands. `secret_env` below is a narrower, different
    # thing: a mapping of {env_var_name: granted_secret_name} -- the LLM
    # agent supplies which of the session's already-granted secret NAMES it
    # wants injected under which env var name, never a literal value. The
    # sidecar resolves each name server-side via the same `_get_secret_value`
    # TTL-cached lookup `http_request_tool.py`'s `{{secret:name}}`
    # substitution already uses, and merges the resolved values into the
    # spawned process's environment directly -- never templated into
    # `command`, so it can never appear in this route's own command-string
    # log truncation below. A name that isn't in this session's
    # `secret_names` grant list resolves to None and is silently omitted
    # (not injected, not an error) -- same "unresolvable reference" handling
    # `_get_secret_value` already has for the http-request path.
    secret_env: Optional[dict[str, str]] = None


class ExecResponse(BaseModel):
    exit_code: int
    stdout: str
    stderr: str


class InterpreterExecRequest(BaseModel):
    code: str
    timeout: int = 30
    # SECURITY: no `env` field, same rationale as ExecRequest above -- no
    # caller-supplied environment variables are ever injected into the
    # interpreter process.


class InterpreterExecResponse(BaseModel):
    stdout: str
    result: Optional[str] = None
    error: Optional[str] = None
    truncated: bool = False


class InterpreterStatusResponse(BaseModel):
    running: bool
    started_at: Optional[str] = None
    idle_seconds: Optional[float] = None


class InterpreterResetResponse(BaseModel):
    status: str


class NodeInterpreterExecRequest(BaseModel):
    code: str
    timeout: int = 30
    # SECURITY: no `env` field, same rationale as ExecRequest/
    # InterpreterExecRequest above.


class NodeInterpreterExecResponse(BaseModel):
    stdout: str
    result: Optional[str] = None
    error: Optional[str] = None
    truncated: bool = False


class NodeInterpreterStatusResponse(BaseModel):
    running: bool
    started_at: Optional[str] = None
    idle_seconds: Optional[float] = None


class NodeInterpreterResetResponse(BaseModel):
    status: str


# docs/BROWSER-EXEC-DESIGN.md §2's four narrow primitives.
_BROWSER_ALLOWED_WAIT_UNTIL = {"load", "domcontentloaded", "networkidle", "commit"}


class BrowserNavigateRequest(BaseModel):
    url: str
    wait_until: str = "load"
    timeout_seconds: int = 30


class BrowserNavigateResponse(BaseModel):
    title: Optional[str] = None
    url: Optional[str] = None
    status: Optional[int] = None
    error: Optional[str] = None


class BrowserExecRequest(BaseModel):
    script: str
    timeout_seconds: int = 10
    # SECURITY: no `env`/host-code-execution field of any kind -- this is
    # page-context JS evaluation only (Playwright's page.evaluate, i.e. CDP
    # Runtime.evaluate), scoped to whatever the loaded page can already do
    # in a browser. See docs/BROWSER-EXEC-DESIGN.md §2.


class BrowserExecResponse(BaseModel):
    # The evaluated script's JSON-serializable completion value (Playwright
    # deserializes it from CDP already) -- unlike NodeInterpreterExecResponse,
    # this is NOT a util.inspect() string repr, since there is no equivalent
    # "persistent REPL history/typeof" concern here (design doc §2).
    result: Any = None
    error: Optional[str] = None


class BrowserScreenshotRequest(BaseModel):
    full_page: bool = False


class BrowserScreenshotResponse(BaseModel):
    image_base64: Optional[str] = None
    error: Optional[str] = None


class BrowserCloseResponse(BaseModel):
    status: str


FILE_CONTENT_MAX_LENGTH = 10 * 1024 * 1024  # 10MB of characters; kept in sync
# with the control-plane's SANDBOX_FILE_CONTENT_MAX_LENGTH (schemas.py) so a
# request accepted at that layer is never rejected here.

# /view's final response is truncated to 100KB regardless of input size, but
# an agent-writable file under /workspace/etc. can be arbitrarily large (via
# /exec) -- without a pre-read size check, the sidecar process would buffer
# the ENTIRE file into memory before that truncation ever runs, so a single
# multi-GB file is a memory-exhaustion vector. Generous relative to the
# eventual 100KB truncation since legitimate text files can be much larger
# than what gets shown.
VIEW_MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024  # 25MB


class ProcessStartRequest(BaseModel):
    command: str
    description: Optional[str] = None
    # Required, not optional — see the SANDBOX_PROCESS_MAX_RUNTIME_SECONDS_CEILING
    # comment above: a background process is a standing resource commitment,
    # unlike a bounded /exec call, so it never gets an unbounded default.
    max_runtime_seconds: int
    # SECURITY (docs/NETWORK-INGRESS-DESIGN.md): opt-in port to expose via
    # `/preview/{port}/...` once this process is listening. When set, this
    # one process is spawned WITHOUT the per-exec fresh network namespace
    # (`unshare -n`) that every other exec'd command and background process
    # gets by default -- it stays in the pod's own shared network namespace
    # (the same one the sidecar container itself is in), so the sidecar can
    # reach `127.0.0.1:{expose_port}` to proxy preview requests to it. This
    # is a scoped, explicit exception, not a blanket relaxation: every other
    # /exec and /process/start call (expose_port unset, the default) is
    # completely unaffected and keeps its own fresh, empty network namespace.
    # It also does NOT touch the pod's NetworkPolicy egress/ingress rules --
    # the process can still only reach what the pod's own NetworkPolicy
    # already allows, and nothing outside the cluster can reach this port
    # directly (only the sidecar's own already-authenticated HTTP surface
    # can, via /preview). See SECURITY.md's "Known follow-ups" for the
    # accepted tradeoff this introduces.
    expose_port: Optional[int] = None


class ProcessStartResponse(BaseModel):
    process_id: str
    status: str
    started_at: str


class ProcessOutputResponse(BaseModel):
    status: str
    stdout_chunk: str
    next_offset: int
    truncated: bool
    exit_code: Optional[int] = None


class ProcessInputRequest(BaseModel):
    data: str = Field(max_length=FILE_CONTENT_MAX_LENGTH)


class ProcessInputResponse(BaseModel):
    bytes_written: int


class ProcessStopResponse(BaseModel):
    status: str
    exit_code: Optional[int] = None


class ProcessInfo(BaseModel):
    process_id: str
    command: str
    description: Optional[str] = None
    status: str
    started_at: str
    exit_code: Optional[int] = None
    expose_port: Optional[int] = None


class ProcessListResponse(BaseModel):
    processes: list[ProcessInfo]


class ProcessKillAllResponse(BaseModel):
    killed: int


class FileCreateRequest(BaseModel):
    path: str
    content: str = Field(max_length=FILE_CONTENT_MAX_LENGTH)
    description: Optional[str] = None


class FileCreateResponse(BaseModel):
    path: str
    size: int
    created: bool


class ViewRequest(BaseModel):
    path: str
    view_range: Optional[list[int]] = None  # [start_line, end_line]
    description: Optional[str] = None


class ViewResponse(BaseModel):
    content: str
    lines: int
    is_directory: bool = False
    entries: Optional[list[str]] = None


class ReadImageRequest(BaseModel):
    path: str
    description: Optional[str] = None


class ReadImageResponse(BaseModel):
    path: str
    mime_type: str
    size_bytes: int
    base64_data: str


class StrReplaceRequest(BaseModel):
    path: str
    old_str: str = Field(max_length=FILE_CONTENT_MAX_LENGTH)
    new_str: str = Field(max_length=FILE_CONTENT_MAX_LENGTH)
    replace_all: bool = False
    description: Optional[str] = None


class StrReplaceResponse(BaseModel):
    path: str
    replaced: bool
    occurrences: int


class PresentFilesRequest(BaseModel):
    filepaths: list[str]


class PresentFilesResponse(BaseModel):
    files: list[dict]
    copy_operations: list[str] = Field(default_factory=list)


class ConfigureRequest(BaseModel):
    session_id: Optional[str] = None
    organization_id: Optional[str] = None
    work_item_id: Optional[str] = None
    storage_prefix: Optional[str] = None  # Renamed from s3_prefix
    upload_file_ids: Optional[list[str]] = None
    # Secrets broker grants (docs/SECRETS-DESIGN.md §3/4) -- non-sensitive
    # metadata only, never the secret values themselves. All optional/empty
    # by default: a session with no secret_names simply can't use
    # /http-request's {{secret:...}} substitution at all.
    secret_names: Optional[list[str]] = None
    secret_allowed_hosts: Optional[dict[str, list[str]]] = None
    secret_capability_token: Optional[str] = None
    secrets_control_plane_url: Optional[str] = None


class ConfigureResponse(BaseModel):
    status: str
    session_id: Optional[str]
    prefetched_files: list[str]


class PrefetchUploadsRequest(BaseModel):
    organization_id: Optional[str] = None
    upload_file_ids: Optional[list[str]] = None


class PrefetchUploadsResponse(BaseModel):
    status: str
    session_id: Optional[str]
    prefetched_files: list[str]


class EnsureSkillsRequest(BaseModel):
    skills: list[dict]
    skills_rev: Optional[str] = None


class EnsureSkillsResponse(BaseModel):
    status: str
    changed: bool
    skills_rev: str
    skills_injected: int
    files_written: int


class LsRequest(BaseModel):
    path: str = "/"


class LsResponse(BaseModel):
    entries: list[dict]


class GlobRequest(BaseModel):
    pattern: str
    path: str = "/"


class GlobResponse(BaseModel):
    matches: list[dict]


class GrepRequest(BaseModel):
    pattern: str
    path: Optional[str] = "/"
    glob: Optional[str] = None
    max_matches: int = 500


class GrepResponse(BaseModel):
    matches: list[dict]
    error: Optional[str] = None
    truncated: bool = False


class HttpRequestRequest(BaseModel):
    """Secrets-broker HTTP request (docs/SECRETS-DESIGN.md §3). `headers`/
    `body` may contain literal `{{secret:<name>}}` tokens -- substituted for
    the real value in-process, here, before the real request is sent."""

    method: str = Field(default="GET", max_length=10)
    url: str = Field(min_length=1, max_length=4096)
    headers: dict[str, str] = Field(default_factory=dict)
    body: Optional[str] = Field(default=None, max_length=FILE_CONTENT_MAX_LENGTH)
    timeout: int = Field(default=15, ge=1, le=60)


class HttpRequestResponse(BaseModel):
    status_code: int
    headers: dict[str, str]
    body: str
    truncated: bool = False


class PtyExecRequest(BaseModel):
    command: str
    input_bytes: str = ""  # base64-encoded, may be empty
    timeout_seconds: float = 30.0


class PtyExecResponse(BaseModel):
    output: str
    exit_code: Optional[int]
    timed_out: bool


class LspStartRequest(BaseModel):
    language: str


class LspStartResponse(BaseModel):
    lsp_id: str


class LspOpenRequest(BaseModel):
    path: str
    content: str = Field(max_length=FILE_CONTENT_MAX_LENGTH)


class LspOpenResponse(BaseModel):
    status: str


class LspCompletionRequest(BaseModel):
    path: str
    line: int
    character: int


class LspCompletionResponse(BaseModel):
    items: list[dict] = Field(default_factory=list)


class LspStopResponse(BaseModel):
    status: str


# ============================================================================
# Read-only S3 FUSE bucket mounting (docs/EXTERNAL-STORAGE-MOUNTING-DESIGN.md,
# §2.2 option 2 -- mounted from the sidecar container, which already holds
# CAP_SYS_ADMIN for nsenter, so this needs no NEW capability grant).
#
# What this section deliberately does NOT do: actually perform a live
# mount. Three separate, unreviewed things would need to happen first,
# each requiring its own maintainer sign-off per SECURITY.md's bar for new
# attack surface, none of which this pass attempts:
#   1. /dev/fuse device access granted to the sidecar container in
#      deploy/pod-template.yaml (not present today -- CAP_SYS_ADMIN alone
#      does not grant access to a device node that isn't mounted into the
#      container at all).
#   2. An actual FUSE client binary (s3fs-fuse, goofys, or rclone --
#      s3fs-fuse chosen here as the most mature single-cloud option, per
#      the design doc's "read-only, single-provider first" recommendation)
#      installed in deploy/sidecar.Dockerfile -- not present today.
#   3. A credential-handling decision for the bucket's own access
#      key/secret (a NEW credential type entering the pod, per the design
#      doc's §5) -- not scoped here at all.
# What IS real and unit-tested below: the command-construction logic
# itself (build_s3fs_mount_command/build_s3fs_unmount_command), so that
# once 1-3 above are each explicitly reviewed and done, wiring up the
# route is a small, mechanical change rather than starting from zero.
# ============================================================================

FUSE_DEVICE_PATH = "/dev/fuse"


class MountBucketRequest(BaseModel):
    bucket: str
    mount_path: str
    region: str = "us-east-1"


class MountBucketResponse(BaseModel):
    mounted: bool
    detail: str


EXEC_MAX_STDOUT_BYTES = 1024 * 1024  # 1MB limit
EXEC_MAX_STDERR_BYTES = 256 * 1024  # 256KB limit
_EXEC_READ_CHUNK_SIZE = 64 * 1024


# ============================================================================
# Background process registry (/process/*)
#
# See docs/PROCESS-SESSIONS-DESIGN.md. Unlike every other route in this file,
# /process/start does not await its subprocess to completion — it hands back
# a `process_id` immediately and keeps the asyncio.subprocess.Process handle
# alive in `_process_registry` so later HTTP calls (/output, /input, /stop)
# can interact with the same OS process across multiple requests. This is the
# first genuinely new in-memory-state-that-outlives-a-request pattern in this
# codebase, so process teardown is deliberately explicit and paranoid: see
# `_kill_all_processes()`, called from both `/configure` (pod recycle/claim)
# and graceful shutdown, closing the cross-tenant leak this feature would
# otherwise introduce (a process — and its buffered stdout — started by one
# tenant, still alive or still readable, once a recycled pod is claimed by a
# different tenant).
# ============================================================================

_process_registry: dict[str, "ProcessHandle"] = {}
_process_registry_lock: Optional[asyncio.Lock] = None

# port -> process_id, for processes started with expose_port set (see
# docs/NETWORK-INGRESS-DESIGN.md). Guarded by the same registry lock as
# _process_registry -- /preview only ever proxies to a port present here.
_exposed_ports: dict[int, str] = {}


# ============================================================================
# API Endpoints
# ============================================================================

@app.get("/health")
async def health():
    """Health check endpoint."""
    idle_seconds = _time.monotonic() - _last_activity_at
    return {
        "status": "healthy",
        "session_id": current_session["session_id"],
        "skills_rev": current_session.get("skills_rev"),
        "runtime_mode": RUNTIME_MODE,
        "storage_backend": STORAGE_BACKEND,
        "idle_seconds": round(idle_seconds, 1),
    }


import ipaddress
import re as _re
import socket as _socket


class WatchDirectoryRequest(BaseModel):
    path: str = "/"
    timeout_seconds: float = 10.0


class WatchDirectoryChange(BaseModel):
    path: str
    event: str


class WatchDirectoryResponse(BaseModel):
    changes: list[WatchDirectoryChange]
    timed_out: bool


# ============================================================================
# Tool Call Proxy — Code Execution Mode
# ============================================================================
# Receives tool call requests from sandbox code (via _tool_bridge.py) and
# forwards them to the backend API for execution against real MCP tools.

class ToolCallRequest(BaseModel):
    tool_name: str
    arguments: dict = {}
    # SECURITY: intentionally accepted but IGNORED. This field exists only
    # for backwards compatibility with older callers that used to send it.
    # The backend's internal tool-call endpoint must only ever be told the
    # sidecar's own `current_session["session_id"]` — never a value supplied
    # by sandboxed/LLM-generated code (via the sandbox-side tool bridge).
    # Honoring a caller-supplied session_id here would let sandboxed code
    # impersonate an arbitrary other session/tenant when invoking backend
    # tools. See tests/test_sidecar_tool_call_session_id.py.
    session_id: Optional[str] = None


# ============================================================================
# Startup / Shutdown
# ============================================================================

_DOCKER_SOCKET_PATH = "/var/run/docker.sock"


def _warn_if_docker_socket_mounted() -> None:
    """Regression tripwire, not an expected condition: this sidecar has not
    needed /var/run/docker.sock since deploy/docker-compose.yml switched
    compose-mode exec from `docker exec` to nsenter (sharing the sandbox
    container's PID namespace instead) -- see that file's own comment on
    why the socket mount was removed outright, and SECURITY.md's history of
    the CRITICAL host-root-escape risk it used to carry. Neither the
    Kubernetes runtime (deploy/pod-template.yaml) nor deploy/docker-compose.yml
    mount this socket anymore, so this warning firing at all means something
    (a custom deployment, a local override) reintroduced it -- treat it as a
    bug to fix, not a known/accepted trade-off.
    """
    try:
        socket_exists = stat.S_ISSOCK(os.stat(_DOCKER_SOCKET_PATH).st_mode)
    except OSError:
        return
    if not socket_exists:
        return

    banner = "\n".join([
        "",
        "!" * 78,
        "! CRITICAL: /var/run/docker.sock is mounted into this container.",
        "!",
        "! Anyone with a live connection to this socket can escalate to full",
        "! HOST-ROOT compromise, e.g. `docker run --privileged -v /:/host ...`.",
        "! There is NO implemented mitigation for this beyond SIDECAR_AUTH_TOKEN",
        "! gating this sidecar's own HTTP API.",
        "!",
        "! Neither deploy/pod-template.yaml nor deploy/docker-compose.yml mounts",
        "! this socket -- this sidecar no longer uses it at all (exec goes",
        "! through nsenter now, not `docker exec`). Seeing this warning means",
        "! whatever deployment config started this container reintroduced the",
        "! mount -- remove it; there is no supported reason to keep it.",
        "!" * 78,
        "",
    ])
    logger.warning(banner)


@app.on_event("startup")
async def startup_event():
    """Start periodic sync worker."""
    global _periodic_sync_task

    _warn_if_docker_socket_mounted()

    # See docs/PROCESS-SESSIONS-DESIGN.md section 2(b): a hard crash/OOM-kill
    # of the sidecar's own process bypasses shutdown_event's graceful
    # _kill_all_processes() call entirely, and a real tested experiment
    # confirmed the underlying OS process survives that as a genuine,
    # running orphan. Run the sweep before anything else at startup so a
    # freshly-recycled or freshly-restarted sidecar doesn't leave one
    # sitting around any longer than necessary.
    reaped = _sweep_orphaned_background_processes()
    if reaped:
        logger.warning(
            f"[startup] Reaped {reaped} orphaned background process group(s) "
            "from a previous sidecar incarnation"
        )

    if _periodic_sync_task is None or _periodic_sync_task.done():
        _periodic_sync_task = asyncio.create_task(_periodic_sync_loop())
        logger.info(
            "[startup] Started periodic sync loop "
            f"(flush_interval={SYNC_FLUSH_INTERVAL_SEC}s, reconcile_interval={SYNC_RECONCILE_INTERVAL_SEC}s)"
        )


@app.on_event("shutdown")
async def shutdown_event():
    """Stop sync worker and flush outputs on graceful shutdown."""
    global _periodic_sync_task

    # No claim of surviving a sidecar container restart (see
    # docs/PROCESS-SESSIONS-DESIGN.md section 2(b)) -- a graceful shutdown at
    # least gets a clean kill of every tracked background process instead of
    # leaving orphans behind for the container runtime to sort out.
    logger.info("[shutdown] Killing tracked background processes...")
    await _kill_all_processes()

    logger.info("[shutdown] Stopping sync worker...")
    if _periodic_sync_task is not None:
        _periodic_sync_task.cancel()
        await asyncio.gather(_periodic_sync_task, return_exceptions=True)

    _periodic_sync_task = None

    logger.info("[shutdown] Flushing outputs before shutdown...")
    try:
        await asyncio.wait_for(
            flush_outputs(reason="shutdown", discover_untracked=True, warn_if_no_session=False),
            timeout=SYNC_SHUTDOWN_FLUSH_TIMEOUT_SEC,
        )
    except asyncio.TimeoutError:
        logger.warning(
            f"[shutdown] Flush timed out after {SYNC_SHUTDOWN_FLUSH_TIMEOUT_SEC}s; proceeding"
        )

    logger.info("[shutdown] Killing persistent interpreter (if any)...")
    await _reset_interpreter()

    logger.info("[shutdown] Killing persistent Node interpreter (if any)...")
    await _reset_node_interpreter()

    logger.info("[shutdown] Killing browser process (if any)...")
    await _reset_browser()

    logger.info("[shutdown] Killing LSP servers (if any)...")
    await _kill_all_lsp_servers()

    logger.info("[shutdown] Complete")


# ============================================================================
# Main
# ============================================================================


# ============================================================================
# Concern-module wiring (GitHub issue #71 refactor)
#
# The route handlers and helper implementations now live in the sidecar_*
# sibling modules. This file remains the single owner of all module-level
# configuration, mutable state, and Pydantic models; the sibling modules read
# them via ``main.<NAME>`` at call time so tests that monkeypatch attributes on
# ``main`` still take effect. Names that tests or other modules reference are
# re-exported here so ``import main`` keeps exposing the same surface.
# ============================================================================

import sidecar_paths  # noqa: E402
import sidecar_storage  # noqa: E402
import sidecar_execution  # noqa: E402
import sidecar_pty  # noqa: E402
import sidecar_desktop  # noqa: E402
import sidecar_processes  # noqa: E402
import sidecar_interpreter  # noqa: E402
import sidecar_node_interpreter  # noqa: E402
import sidecar_browser  # noqa: E402
import sidecar_lsp  # noqa: E402
import sidecar_secrets  # noqa: E402
import sidecar_files  # noqa: E402
import sidecar_sync  # noqa: E402

from sidecar_paths import (  # noqa: E402,F401
    _is_under_root, _normalize_input_path, _typed_allowed_roots, _ls_allowed_roots,
    _is_path_contained, _resolve_virtual_path, _resolve_ls_path, _path_root,
    _revalidate_path_or_400, _is_read_only_virtual_path, _sanitize_instance_slug,
    _sanitize_rel_file_path, _to_virtual_path, _storage_bucket_for_virtual_path,
)
from sidecar_storage import (  # noqa: E402,F401
    StorageBackend, S3Backend, AzureBlobBackend, get_storage_backend, storage,
    build_s3fs_mount_command, build_s3fs_unmount_command,
)
from sidecar_execution import (  # noqa: E402,F401
    get_sandbox_pid, build_k8s_exec_command, _read_stream_bounded, exec_in_sandbox,
)
from sidecar_pty import (  # noqa: E402,F401
    build_pty_command, _check_pty_auth, _pty_to_websocket, _websocket_to_pty, _pty_exec_once,
    kill_takeover_tmux_session,
)
from sidecar_desktop import (  # noqa: E402,F401
    _build_desktop_entry_argv, _ensure_desktop_stack_running, kill_desktop_session,
    _check_desktop_auth, _vnc_to_websocket, _websocket_to_vnc,
)
from sidecar_processes import (  # noqa: E402,F401
    ProcessHandle, _get_process_registry_lock, _spawn_background_process,
    _process_reader_loop, _process_watchdog, _stop_process, _release_exposed_port,
    _get_process_or_404, _kill_all_processes, _filtered_proxy_headers,
    _signal_process_group, _sweep_orphaned_background_processes,
    _preview_stream_monotonic, _stream_upstream_body,
)
from sidecar_interpreter import (  # noqa: E402,F401
    _InterpreterHandle, _get_interpreter_lock, _kill_interpreter_handle, _spawn_interpreter,
    _get_or_spawn_interpreter_locked, _reset_interpreter_locked, _reset_interpreter,
    _reap_idle_interpreter, _read_interpreter_response_line, _interpreter_exec_now,
)
from sidecar_node_interpreter import (  # noqa: E402,F401
    _NodeInterpreterHandle, _get_node_interpreter_lock, _kill_node_interpreter_handle,
    _spawn_node_interpreter, _get_or_spawn_node_interpreter_locked,
    _reset_node_interpreter_locked, _reset_node_interpreter, _reap_idle_node_interpreter,
    _node_interpreter_exec_now,
)
from sidecar_browser import (  # noqa: E402,F401
    _BrowserHandle, _get_browser_lock, _kill_browser_handle, _spawn_browser,
    _get_or_spawn_browser_locked, _reset_browser_locked, _reset_browser,
    _reap_idle_browser, _browser_dispatch_now,
)
from sidecar_lsp import (  # noqa: E402,F401
    LspServerHandle, _get_lsp_registry_lock, _get_lsp_handle_or_404,
    _spawn_lsp_server, _kill_lsp_handle, _stop_lsp_handle,
    _kill_all_lsp_servers, _reap_idle_lsp_servers,
)
from sidecar_secrets import (  # noqa: E402,F401
    _embedded_ipv4, _is_disallowed_destination_ip, _resolve_and_validate_destination,
    _host_allowed_for_secret, _get_secret_value, _substitute_secrets, _scrub_secret_values,
)
from sidecar_files import (  # noqa: E402,F401
    _compute_skills_rev, _materialize_skills, _grep_search_sync,
    _parse_inotify_events, _watch_directory_once,
)
from sidecar_sync import (  # noqa: E402,F401
    _get_flush_lock, _scrub_disallowed_pending_sync, _is_ignored_sync_dir_name,
    _trim_synced_signature_cache, _file_signature, _scan_sync_file_signatures,
    _discover_untracked_sync_files, _clear_tmp_session_data, _clear_directory_contents,
    _has_active_sync_session, _queue_virtual_path_for_sync, _wait_for_stable_signature,
    _sync_candidate_paths, flush_outputs, _periodic_sync_loop,
    _prefetch_namespace_from_prefix, prefetch_files, prefetch_uploads_from_prefix,
    prefetch_legacy_uploads,
)

app.include_router(sidecar_storage.router)
app.include_router(sidecar_execution.router)
app.include_router(sidecar_pty.router)
app.include_router(sidecar_desktop.router)
app.include_router(sidecar_processes.router)
app.include_router(sidecar_interpreter.router)
app.include_router(sidecar_node_interpreter.router)
app.include_router(sidecar_browser.router)
app.include_router(sidecar_lsp.router)
app.include_router(sidecar_secrets.router)
app.include_router(sidecar_files.router)
app.include_router(sidecar_sync.router)

if __name__ == "__main__":
    import uvicorn

    if _sidecar_tls_files_present():
        logger.info(f"[startup] Serving HTTPS (cert={SIDECAR_TLS_CERT_PATH})")
        uvicorn.run(
            app,
            host="0.0.0.0",
            port=8080,
            ssl_certfile=SIDECAR_TLS_CERT_PATH,
            ssl_keyfile=SIDECAR_TLS_KEY_PATH,
        )
    else:
        logger.warning(
            "[startup] Serving plain HTTP -- no TLS cert/key mounted at "
            f"{SIDECAR_TLS_MOUNT_PATH} (or {SIDECAR_TLS_DISABLED_ENV}=true). "
            "Manager-to-sidecar traffic is unencrypted; see SECURITY.md."
        )
        uvicorn.run(app, host="0.0.0.0", port=8080)
