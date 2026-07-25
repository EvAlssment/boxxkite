"""Tests for the sidecar's headless-browser automation (/browser/*).

Mirrors tests/test_sidecar_node_interpreter.py's coverage shape (see
docs/BROWSER-EXEC-DESIGN.md):

- 404 when BOXKITE_BROWSER_ENABLED is off (the default).
- Requires the same sidecar auth as every other route once enabled.
- navigate/exec/screenshot lazily spawn the browser process on first call.
- A script-level error (page.evaluate throwing, a bad navigation) comes
  back as a normal 200 with `error` set -- it does NOT kill the process,
  the same "application error vs transport error" split the interpreters
  already have.
- A transport-level failure (timeout, dead process, malformed response)
  DOES kill the process and 502s; the next call respawns.
- /browser/close kills the process; idempotent when nothing is running.
- Idle timeout kills the process (BROWSER_IDLE_TIMEOUT_SECONDS).
- Oversized screenshots are rejected with an error, never silently
  truncated (a truncated PNG is corrupt, unlike truncated text output).
- /configure kills any live browser process before wiping session state,
  unconditionally (regardless of the current BOXKITE_BROWSER_ENABLED value).
- The browser driver subprocess is spawned with
  skip_network_isolation=True in K8s mode (docs/BROWSER-EXEC-DESIGN.md
  §3.1) -- this is the ONE sidecar-launched subprocess that opts out of
  the per-exec empty network namespace, and it must be exactly this one.
- The session exec budget covers /browser/navigate and /browser/exec.

Two tiers of tests:

1. Most tests below replace the production driver script (which
   `require()`s the optional `playwright` npm package and needs a real
   Chromium binary) with a small dependency-free fake driver implementing
   the exact same newline-delimited JSON request/response protocol. This
   lets every plumbing/lifecycle/gating test run deterministically with
   only a plain `node` binary, the same portability every other sidecar
   test already has.

   This is why this file (unlike every sibling test_sidecar_*.py file)
   imports `sidecar_browser` directly in addition to `main` -- the fake
   driver source needs to be substituted for `sidecar_browser`'s own
   module-level `_BROWSER_DRIVER_SOURCE` constant, which `_spawn_browser`
   reads by name at call time (not something `main.py` owns/re-exports,
   since it's driver source code, not shared config/state).

2. A small number of tests (marked and self-skipping) exercise the REAL
   production driver script against a real headless Chromium, when one is
   actually available in this environment (an installed `playwright` npm
   package reachable via NODE_PATH, plus a Chromium/Chrome executable) --
   see `_resolve_playwright_node_path`/`_resolve_test_chromium_executable`
   below. These are a genuine, valuable extra layer of verification where
   available, but the fake-driver tests above are what make this suite
   portable to environments (e.g. plain CI) that have neither.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess

import main as sidecar_main
import pytest
import sidecar_browser
from fastapi.testclient import TestClient

AUTH_TOKEN = "the-real-secret"


def _auth_headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: AUTH_TOKEN}


def _reset_shared_module_state():
    """The session exec budget counters and the browser handle/lock are
    plain module-global mutable state on `main` (not something monkeypatch
    can revert automatically the way an attribute *override* can, since
    tests mutate them in place rather than only replacing them) --
    explicitly reset before and after every test in this file so one test's
    exec-count usage or leftover live process can never leak into the next,
    mirroring tests/test_sidecar_session_exec_budget.py's own
    setup_function/teardown_function pattern."""
    sidecar_main._session_exec_count = 0
    sidecar_main._session_exec_seconds = 0.0
    sidecar_main._session_budget_exceeded = None
    sidecar_main._session_budget_lock = None
    sidecar_main._browser_handle = None
    sidecar_main._browser_lock = None


def setup_function(_):
    _reset_shared_module_state()


def teardown_function(_):
    _reset_shared_module_state()


# A minimal, dependency-free stand-in for the real Playwright-backed driver
# -- implements the exact same protocol (one JSON request per line in,
# {"data":..., "error":...} per line out) without needing the `playwright`
# npm package or a real Chromium binary. Deliberately hangs (never writes a
# response) for a navigate to "https://hang.invalid/" so tests can exercise
# the sidecar's own timeout-kills-the-process path.
_FAKE_BROWSER_DRIVER_SOURCE = """
process.stdout.write("__BOXKITE_BROWSER_READY__\\n");
const readline = require('readline');
const rl = readline.createInterface({ input: process.stdin, terminal: false });
rl.on('line', (rawLine) => {
  const line = rawLine.trim();
  if (!line) return;
  let req;
  try { req = JSON.parse(line); } catch (e) {
    process.stdout.write(JSON.stringify({ data: null, error: 'Invalid request: ' + e }) + '\\n');
    return;
  }
  if (req.action === 'navigate') {
    if (req.url === 'https://hang.invalid/') return;
    if (req.url === 'https://dns-failure.invalid/') {
      process.stdout.write(JSON.stringify({ data: null, error: 'net::ERR_NAME_NOT_RESOLVED' }) + '\\n');
      return;
    }
    process.stdout.write(JSON.stringify({
      data: { title: 'Fake Title', url: req.url, status: 200 }, error: null
    }) + '\\n');
    return;
  }
  if (req.action === 'exec') {
    if (req.script.indexOf('throw') !== -1) {
      process.stdout.write(JSON.stringify({ data: null, error: 'boom' }) + '\\n');
      return;
    }
    let result;
    try { result = eval(req.script); } catch (e) {
      process.stdout.write(JSON.stringify({ data: null, error: String(e.message || e) }) + '\\n');
      return;
    }
    process.stdout.write(JSON.stringify({ data: { result: result === undefined ? null : result }, error: null }) + '\\n');
    return;
  }
  if (req.action === 'screenshot') {
    const size = req.full_page ? 4096 : 16;
    const buf = Buffer.alloc(size, 1);
    if (req.max_bytes && buf.length > req.max_bytes) {
      process.stdout.write(JSON.stringify({
        data: null,
        error: 'Screenshot is ' + buf.length + ' bytes, exceeding the ' + req.max_bytes + '-byte cap'
      }) + '\\n');
      return;
    }
    process.stdout.write(JSON.stringify({ data: { image_base64: buf.toString('base64') }, error: null }) + '\\n');
    return;
  }
  process.stdout.write(JSON.stringify({ data: null, error: 'Unknown action: ' + req.action }) + '\\n');
});
"""


def _enable_fake_driver(monkeypatch):
    """Gate the routes on, bypass nsenter (no real sandbox container in this
    test environment -- same technique test_sidecar_node_interpreter.py
    uses), and substitute the dependency-free fake driver above for the
    real Playwright-backed one."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "BOXKITE_BROWSER_ENABLED", True)
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 1)
    monkeypatch.setattr(
        sidecar_main,
        "build_k8s_exec_command",
        lambda pid, command, **kwargs: ["sh", "-c", command],
    )
    monkeypatch.setattr(sidecar_browser, "_BROWSER_DRIVER_SOURCE", _FAKE_BROWSER_DRIVER_SOURCE)

    node_path = shutil.which("node")
    if node_path:
        node_dir = os.path.dirname(node_path)
        extended_env = dict(sidecar_main.SAFE_EXEC_ENV)
        extended_env["PATH"] = f"{extended_env['PATH']}{os.pathsep}{node_dir}"
        monkeypatch.setattr(sidecar_main, "SAFE_EXEC_ENV", extended_env)


# ---------------------------------------------------------------------------
# Gating / auth
# ---------------------------------------------------------------------------

def test_browser_navigate_404s_when_disabled_by_default(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/navigate", json={"url": "https://example.com"}, headers=_auth_headers()
        )
        assert response.status_code == 404


def test_browser_exec_404s_when_disabled_by_default(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/exec", json={"script": "1+1"}, headers=_auth_headers()
        )
        assert response.status_code == 404


def test_browser_screenshot_404s_when_disabled_by_default(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    with TestClient(sidecar_main.app) as client:
        response = client.post("/browser/screenshot", json={}, headers=_auth_headers())
        assert response.status_code == 404


def test_browser_close_404s_when_disabled_by_default(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    with TestClient(sidecar_main.app) as client:
        response = client.post("/browser/close", headers=_auth_headers())
        assert response.status_code == 404


def test_browser_navigate_requires_auth_like_every_other_route(monkeypatch):
    _enable_fake_driver(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post("/browser/navigate", json={"url": "https://example.com"})
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def test_browser_navigate_rejects_empty_url(monkeypatch):
    _enable_fake_driver(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/navigate", json={"url": "   "}, headers=_auth_headers()
        )
        assert response.status_code == 400


def test_browser_navigate_rejects_invalid_wait_until(monkeypatch):
    _enable_fake_driver(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/navigate",
            json={"url": "https://example.com", "wait_until": "immediately"},
            headers=_auth_headers(),
        )
        assert response.status_code == 400


def test_browser_exec_rejects_empty_script(monkeypatch):
    _enable_fake_driver(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/exec", json={"script": "  "}, headers=_auth_headers()
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# Core request/response behavior (fake driver)
# ---------------------------------------------------------------------------

def test_browser_navigate_returns_title_url_status(monkeypatch):
    _enable_fake_driver(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/navigate",
            json={"url": "https://example.com/page", "wait_until": "load"},
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["title"] == "Fake Title"
        assert body["url"] == "https://example.com/page"
        assert body["status"] == 200
        assert body["error"] is None


def test_browser_navigate_reports_application_level_error_as_200_not_502(monkeypatch):
    """A page-level navigation failure (DNS, bad redirect, ...) is an
    application error like the interpreters' own thrown-exception errors --
    it must NOT be conflated with a transport/process failure (502)."""
    _enable_fake_driver(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/navigate",
            json={"url": "https://dns-failure.invalid/"},
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["error"] == "net::ERR_NAME_NOT_RESOLVED"


def test_browser_exec_returns_json_serializable_result(monkeypatch):
    _enable_fake_driver(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/exec", json={"script": "21 * 2"}, headers=_auth_headers()
        )
        assert response.status_code == 200
        body = response.json()
        assert body["result"] == 42
        assert body["error"] is None


def test_browser_exec_reports_errors_without_killing_the_process(monkeypatch):
    """Mirrors test_node_interpreter_reports_errors_without_losing_state --
    a script-level error must not tear down the kept-alive process."""
    _enable_fake_driver(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        error_response = client.post(
            "/browser/exec", json={"script": "throw new Error('boom')"}, headers=_auth_headers()
        )
        assert error_response.status_code == 200
        assert error_response.json()["error"] == "boom"

        # The SAME underlying process must still be alive and reused, not
        # respawned, for the next call.
        handle_after_error = sidecar_main._browser_handle
        assert handle_after_error is not None
        assert handle_after_error.proc.returncode is None

        follow_up = client.post(
            "/browser/exec", json={"script": "1 + 1"}, headers=_auth_headers()
        )
        assert follow_up.json()["result"] == 2
        assert sidecar_main._browser_handle is handle_after_error


def test_browser_screenshot_returns_base64_png_payload(monkeypatch):
    _enable_fake_driver(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/screenshot", json={"full_page": False}, headers=_auth_headers()
        )
        assert response.status_code == 200
        body = response.json()
        assert body["error"] is None
        assert body["image_base64"]
        # Round-trips to valid bytes -- not corrupt/truncated.
        raw = base64.b64decode(body["image_base64"])
        assert len(raw) == 16


def test_browser_screenshot_rejects_oversized_payload_instead_of_truncating(monkeypatch):
    """Unlike text output (safely truncatable mid-stream), a truncated PNG
    is corrupt -- an oversized screenshot must be rejected with an error,
    never silently cut down to the byte cap."""
    _enable_fake_driver(monkeypatch)
    # The fake driver's full_page screenshot is 4096 bytes (see
    # _FAKE_BROWSER_DRIVER_SOURCE) -- cap well below that.
    monkeypatch.setattr(sidecar_main, "BROWSER_MAX_SCREENSHOT_BYTES", 1024)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/screenshot", json={"full_page": True}, headers=_auth_headers()
        )
        assert response.status_code == 200
        body = response.json()
        assert body["image_base64"] is None
        assert body["error"] is not None
        assert "exceeding" in body["error"]


def test_browser_close_kills_the_process_and_is_idempotent(monkeypatch):
    _enable_fake_driver(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        # Idempotent even when nothing is running yet.
        first_close = client.post("/browser/close", headers=_auth_headers())
        assert first_close.status_code == 200
        assert first_close.json() == {"status": "closed"}

        client.post("/browser/navigate", json={"url": "https://example.com"}, headers=_auth_headers())
        live_handle = sidecar_main._browser_handle
        assert live_handle is not None
        assert live_handle.proc.returncode is None

        second_close = client.post("/browser/close", headers=_auth_headers())
        assert second_close.status_code == 200
        assert sidecar_main._browser_handle is None
        assert live_handle.proc.returncode is not None

        # Next navigate starts a fresh process.
        response = client.post(
            "/browser/navigate", json={"url": "https://example.com"}, headers=_auth_headers()
        )
        assert response.status_code == 200
        assert sidecar_main._browser_handle is not None
        assert sidecar_main._browser_handle is not live_handle


def test_browser_navigate_timeout_kills_process_and_respawns_next_call(monkeypatch):
    """A call that never gets a response (transport-level failure) must
    kill the whole process and 502 -- unlike an application-level error,
    which leaves the process alive (see the "without killing" test above).
    The next call must transparently spawn a fresh one."""
    _enable_fake_driver(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/navigate",
            json={"url": "https://hang.invalid/", "timeout_seconds": 1},
            headers=_auth_headers(),
        )
        assert response.status_code == 502
        assert sidecar_main._browser_handle is None

        follow_up = client.post(
            "/browser/navigate", json={"url": "https://example.com"}, headers=_auth_headers()
        )
        assert follow_up.status_code == 200
        assert follow_up.json()["title"] == "Fake Title"


def test_spawn_browser_fails_when_sandbox_process_is_missing(monkeypatch):
    """K8s mode: if get_sandbox_pid() can't find the sandbox process,
    /browser/navigate must fail loudly (502), not hang or silently no-op."""
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "BOXKITE_BROWSER_ENABLED", True)
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: None)

    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/navigate", json={"url": "https://example.com"}, headers=_auth_headers()
        )
        assert response.status_code == 502


# ---------------------------------------------------------------------------
# Idle reaping / configure teardown
# ---------------------------------------------------------------------------

async def test_browser_idle_reaper_kills_process_past_timeout(monkeypatch):
    """Drives _get_or_spawn_browser_locked/_reap_idle_browser directly (no
    TestClient/HTTP) so this coroutine and the subprocess it awaits share
    one event loop -- same reasoning
    test_node_interpreter_idle_reaper_kills_process_past_timeout gives."""
    _enable_fake_driver(monkeypatch)
    monkeypatch.setattr(sidecar_main, "BROWSER_IDLE_TIMEOUT_SECONDS", 0)

    async with sidecar_main._get_browser_lock():
        handle = await sidecar_main._get_or_spawn_browser_locked()
    assert handle.proc.returncode is None

    await sidecar_main._reap_idle_browser()

    assert sidecar_main._browser_handle is None
    assert handle.proc.returncode is not None


def test_configure_kills_live_browser_before_wiping_session(monkeypatch, tmp_path):
    """Regression test mirroring
    test_configure_kills_live_node_interpreter_before_wiping_session: a
    recycled pod must never hand a new tenant a still-live browser process
    (and whatever page/cookies/state it holds) left over from the previous
    tenant."""
    _enable_fake_driver(monkeypatch)

    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(sidecar_main, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(sidecar_main, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(sidecar_main, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(sidecar_main, "TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(sidecar_main.os, "chown", lambda *args, **kwargs: None)

    with TestClient(sidecar_main.app) as client:
        client.post(
            "/browser/navigate", json={"url": "https://example.com"}, headers=_auth_headers()
        )
        live_handle = sidecar_main._browser_handle
        assert live_handle is not None
        assert live_handle.proc.returncode is None

        configure_response = client.post(
            "/configure",
            json={
                "session_id": None,
                "organization_id": None,
                "work_item_id": None,
                "storage_prefix": None,
            },
            headers=_auth_headers(),
        )
        assert configure_response.status_code == 200

        assert sidecar_main._browser_handle is None
        assert live_handle.proc.returncode is not None


def test_configure_kills_live_browser_even_when_flag_is_off(monkeypatch, tmp_path):
    """docs/BROWSER-EXEC-DESIGN.md §4: /configure must kill any live browser
    process UNCONDITIONALLY -- a process started while
    BOXKITE_BROWSER_ENABLED was true must still be killed if the flag was
    since flipped off before this recycle, mirroring the Node
    interpreter's identical requirement."""
    _enable_fake_driver(monkeypatch)

    monkeypatch.setattr(sidecar_main, "WORKSPACE_DIR", str(tmp_path / "workspace"))
    monkeypatch.setattr(sidecar_main, "OUTPUTS_DIR", str(tmp_path / "outputs"))
    monkeypatch.setattr(sidecar_main, "UPLOADS_DIR", str(tmp_path / "uploads"))
    monkeypatch.setattr(sidecar_main, "SKILLS_DIR", str(tmp_path / "skills"))
    monkeypatch.setattr(sidecar_main, "TMP_DIR", str(tmp_path / "tmp"))
    monkeypatch.setattr(sidecar_main.os, "chown", lambda *args, **kwargs: None)

    with TestClient(sidecar_main.app) as client:
        client.post(
            "/browser/navigate", json={"url": "https://example.com"}, headers=_auth_headers()
        )
        live_handle = sidecar_main._browser_handle
        assert live_handle is not None

        # Flip the flag off -- /browser/navigate itself would now 404 --
        # but /configure must still tear down the leftover process.
        monkeypatch.setattr(sidecar_main, "BOXKITE_BROWSER_ENABLED", False)

        configure_response = client.post(
            "/configure",
            json={
                "session_id": None,
                "organization_id": None,
                "work_item_id": None,
                "storage_prefix": None,
            },
            headers=_auth_headers(),
        )
        assert configure_response.status_code == 200
        assert sidecar_main._browser_handle is None
        assert live_handle.proc.returncode is not None


# ---------------------------------------------------------------------------
# Network isolation -- docs/BROWSER-EXEC-DESIGN.md §3.1
# ---------------------------------------------------------------------------

def test_spawn_browser_passes_skip_network_isolation_true_in_k8s_mode(monkeypatch):
    """THE security-critical wiring check for §3.1: the browser driver's
    spawn command -- and ONLY the browser driver's -- must be built with
    skip_network_isolation=True. Every other exec/interpreter/process call
    must keep getting the default (isolated) per-exec network namespace;
    this is verified by asserting the kwarg is passed explicitly on this
    call, not merely that navigate succeeds."""
    _enable_fake_driver(monkeypatch)

    calls = []

    def _recording_build_k8s_exec_command(pid, command, **kwargs):
        calls.append(kwargs)
        return ["sh", "-c", command]

    monkeypatch.setattr(sidecar_main, "build_k8s_exec_command", _recording_build_k8s_exec_command)

    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/navigate", json={"url": "https://example.com"}, headers=_auth_headers()
        )
        assert response.status_code == 200

    assert len(calls) == 1
    assert calls[0] == {"skip_network_isolation": True}


def test_build_k8s_exec_command_skip_network_isolation_actually_omits_unshare(monkeypatch):
    """Direct unit check on the primitive itself (sidecar_execution.py) --
    confirms skip_network_isolation=True really does produce a command
    without the `unshare -n` empty-network-namespace wrapper, the same
    primitive expose_port/git-tools already rely on for their own narrow
    exceptions."""
    monkeypatch.setattr(sidecar_main, "SANDBOX_EXEC_NETWORK_ISOLATION_ENABLED", True)
    isolated = sidecar_main.build_k8s_exec_command(123, "echo hi")
    skipped = sidecar_main.build_k8s_exec_command(123, "echo hi", skip_network_isolation=True)

    assert isolated[0] == "unshare"
    assert skipped[0] != "unshare"
    assert "nsenter" in skipped


# ---------------------------------------------------------------------------
# Session exec budget coverage (GitHub issue #122) -- browser_navigate and
# browser_exec must count toward the same shared budget every other
# exec-like route does.
# ---------------------------------------------------------------------------

def test_browser_navigate_calls_count_toward_the_shared_exec_count_budget(monkeypatch):
    _enable_fake_driver(monkeypatch)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_COUNT", 1)
    monkeypatch.setattr(sidecar_main, "SANDBOX_SESSION_MAX_EXEC_SECONDS", 0.0)

    with TestClient(sidecar_main.app) as client:
        first = client.post(
            "/browser/navigate", json={"url": "https://example.com"}, headers=_auth_headers()
        )
        assert first.status_code == 200

        second = client.post(
            "/browser/navigate", json={"url": "https://example.com"}, headers=_auth_headers()
        )
        assert second.status_code == 403
        assert second.json()["detail"]["reason"] == "exec_count"


def test_browser_exec_blocked_by_exhausted_budget_never_spawns_the_browser(monkeypatch):
    """Mirrors test_budget_exceeded_via_exec_blocks_interpreter_exec_without_spawning
    -- a session that already tripped the sticky budget flag must not be
    able to start a brand-new browser process via /browser/exec either."""
    _enable_fake_driver(monkeypatch)
    monkeypatch.setattr(
        sidecar_main,
        "_session_budget_exceeded",
        {"reason": "exec_count", "limit": 1, "used": 1},
    )

    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/exec", json={"script": "1+1"}, headers=_auth_headers()
        )
        assert response.status_code == 403
        assert sidecar_main._browser_handle is None


# ---------------------------------------------------------------------------
# Real-browser tests (self-skipping when this environment has neither a
# reachable `playwright` npm package nor a real Chromium/Chrome binary) --
# see this module's docstring, tier 2.
# ---------------------------------------------------------------------------

def _resolve_playwright_node_path() -> "str | None":
    node_bin = shutil.which("node")
    if not node_bin:
        return None
    for candidate in (
        os.environ.get("BOXKITE_TEST_PLAYWRIGHT_NODE_PATH"),
        "/tmp/pw-test/node_modules",
    ):
        if not candidate or not os.path.isdir(candidate):
            continue
        try:
            result = subprocess.run(
                [node_bin, "-e", "require.resolve('playwright')"],
                env={**os.environ, "NODE_PATH": candidate},
                capture_output=True,
                timeout=10,
            )
        except Exception:
            continue
        if result.returncode == 0:
            return candidate
    return None


def _resolve_test_chromium_executable() -> "str | None":
    for candidate in (
        os.environ.get("BOXKITE_TEST_CHROMIUM_EXECUTABLE"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        shutil.which("chromium"),
        shutil.which("chromium-browser"),
        shutil.which("google-chrome"),
    ):
        if candidate and os.path.exists(candidate):
            return candidate
    return None


_REAL_BROWSER_NODE_PATH = _resolve_playwright_node_path()
_REAL_BROWSER_EXECUTABLE = _resolve_test_chromium_executable()
_REAL_BROWSER_AVAILABLE = _REAL_BROWSER_NODE_PATH is not None and _REAL_BROWSER_EXECUTABLE is not None
_REAL_BROWSER_SKIP_REASON = (
    "No `playwright` npm package reachable via NODE_PATH and/or no Chromium/Chrome "
    "executable found in this environment -- set BOXKITE_TEST_PLAYWRIGHT_NODE_PATH "
    "and BOXKITE_TEST_CHROMIUM_EXECUTABLE to enable these real-browser checks."
)


def _enable_real_browser(monkeypatch):
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "BOXKITE_BROWSER_ENABLED", True)
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 1)
    monkeypatch.setattr(
        sidecar_main,
        "build_k8s_exec_command",
        lambda pid, command, **kwargs: ["sh", "-c", command],
    )

    extended_env = dict(sidecar_main.SAFE_EXEC_ENV)
    node_path = shutil.which("node")
    if node_path:
        node_dir = os.path.dirname(node_path)
        extended_env["PATH"] = f"{extended_env['PATH']}{os.pathsep}{node_dir}"
    extended_env["NODE_PATH"] = _REAL_BROWSER_NODE_PATH
    extended_env["BOXKITE_BROWSER_EXECUTABLE_PATH"] = _REAL_BROWSER_EXECUTABLE
    monkeypatch.setattr(sidecar_main, "SAFE_EXEC_ENV", extended_env)
    monkeypatch.setattr(sidecar_main, "BROWSER_STARTUP_TIMEOUT_SECONDS", 60)


@pytest.mark.skipif(not _REAL_BROWSER_AVAILABLE, reason=_REAL_BROWSER_SKIP_REASON)
def test_browser_navigate_exec_screenshot_against_a_real_headless_chromium(monkeypatch):
    """End-to-end proof, against a REAL headless Chromium, of the exact
    primitives docs/BROWSER-EXEC-DESIGN.md §2 describes: navigate loads a
    real page, exec evaluates real DOM script in that page's JS context,
    screenshot returns a real decodable PNG, close tears the process down.
    """
    _enable_real_browser(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        nav = client.post(
            "/browser/navigate",
            json={"url": "https://example.com", "wait_until": "domcontentloaded", "timeout_seconds": 30},
            headers=_auth_headers(),
        )
        assert nav.status_code == 200
        nav_body = nav.json()
        assert nav_body["error"] is None
        assert nav_body["title"] == "Example Domain"
        assert nav_body["status"] == 200

        ex = client.post(
            "/browser/exec",
            json={"script": "document.querySelectorAll('h1').length", "timeout_seconds": 15},
            headers=_auth_headers(),
        )
        assert ex.status_code == 200
        ex_body = ex.json()
        assert ex_body["error"] is None
        assert ex_body["result"] == 1

        shot = client.post("/browser/screenshot", json={"full_page": False}, headers=_auth_headers())
        assert shot.status_code == 200
        shot_body = shot.json()
        assert shot_body["error"] is None
        raw_png = base64.b64decode(shot_body["image_base64"])
        assert raw_png[:8] == b"\x89PNG\r\n\x1a\n"

        closed = client.post("/browser/close", headers=_auth_headers())
        assert closed.status_code == 200
        assert closed.json() == {"status": "closed"}
        assert sidecar_main._browser_handle is None


@pytest.mark.skipif(not _REAL_BROWSER_AVAILABLE, reason=_REAL_BROWSER_SKIP_REASON)
def test_browser_navigate_against_real_chromium_reports_dns_failure_as_application_error(monkeypatch):
    _enable_real_browser(monkeypatch)
    with TestClient(sidecar_main.app) as client:
        response = client.post(
            "/browser/navigate",
            json={"url": "https://this-host-does-not-exist.invalid./", "timeout_seconds": 15},
            headers=_auth_headers(),
        )
        assert response.status_code == 200
        body = response.json()
        assert body["error"] is not None
