"""Tests for background-process survival across a sidecar crash/restart —
see docs/PROCESS-SESSIONS-DESIGN.md section 2(b) and SECURITY.md's
"not yet verified" entry, both resolved by a real, tested experiment
(GitHub issue #76).

Real containers (actual `nsenter -t <pid> -m -p ...`, actual Linux
PID-namespace reparenting via two containers sharing one PID namespace the
same way Kubernetes' `shareProcessNamespace: true` does) were used during
this issue's investigation to establish the underlying kernel/nsenter
mechanics this fix depends on:

  1. `nsenter -p` forks internally to actually enter the target PID
     namespace, so the `asyncio.subprocess.Process` handle the sidecar
     tracks (nsenter itself) is NOT the same OS process as the sandboxed
     command it wraps. Signalling only the tracked PID left the real
     command alive, running, untouched.
  2. A hard SIGKILL of the container standing in for the sidecar (no
     graceful shutdown, matching an OOM-kill or crash) left that same real
     command running as a genuine orphan -- not a zombie, fully alive,
     reparented within the shared PID namespace, invisible to a
     freshly-started sidecar's own empty process registry.
  3. Forming a process group at spawn time (`start_new_session=True`) and
     signalling the whole group (`os.killpg`) reliably reaches both nsenter
     and the real command it wraps.

This file doesn't require Docker (kept fast/deterministic/CI-safe): it
exercises the same mechanism -- a wrapper process that `os.fork()`s a real
"grandchild" OS process without propagating signals to it (mirroring
nsenter's real fork-without-setpgid shape) -- using plain OS processes,
which reproduces the same kernel-level signal/process-group/orphan
semantics the container experiment above established. A real fork(2) is
used deliberately instead of shell `(...) &` backgrounding: some `/bin/sh`
implementations optimize away the fork for a backgrounded subshell whose
only content is a single `exec`, which would silently make the "grandchild"
the very same OS process as the parent and defeat the point of these tests.

The `_sweep_orphaned_background_processes` tests are Linux-only (they read
`/proc/<pid>/environ`, which doesn't exist on macOS) -- they skip on
platforms without `/proc` and run for real in this repo's Linux CI, which
also matches the sidecar's actual runtime (Chainguard/wolfi containers are
always Linux).
"""

import asyncio
import os
import signal
import sys

import main as sidecar_main
import pytest
from fastapi.testclient import TestClient

AUTH_TOKEN = "the-real-secret"

requires_proc = pytest.mark.skipif(
    not os.path.isdir("/proc"), reason="requires /proc (Linux only, matches the sidecar's real runtime)"
)


def _client() -> TestClient:
    return TestClient(sidecar_main.app).__enter__()


def _headers() -> dict:
    return {sidecar_main.SIDECAR_AUTH_HEADER: AUTH_TOKEN}


def _reset_registry():
    sidecar_main._process_registry.clear()
    sidecar_main._exposed_ports.clear()
    sidecar_main._process_registry_lock = None


def setup_function(_):
    _reset_registry()


def teardown_function(_):
    _reset_registry()


def _fork_and_sleep_script(marker_file, inner_command: str = "sleep 60") -> str:
    """A tiny Python driver that does a REAL `os.fork()`: the child writes
    its own PID to `marker_file` then execs `inner_command`, while the
    parent independently sleeps. Both end up in the same process group
    (fork() doesn't change it) -- this precisely mirrors nsenter's own
    verified shape (an outer process, one internal fork, no setpgid on the
    result) without depending on any shell's background-job optimizations,
    which were observed to sometimes elide the fork entirely for a
    parenthesized subshell containing a single exec'able command.
    """
    args = inner_command.split()
    return (
        "import os, time\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        f"    with open({str(marker_file)!r}, 'w') as f:\n"
        "        f.write(str(os.getpid()))\n"
        f"    os.execvp({args[0]!r}, {args!r})\n"
        "else:\n"
        "    time.sleep(60)\n"
    )


async def _spawn_fork_tree(marker_file, *, env=None, inner_command: str = "sleep 60"):
    """Spawn the fork-tree fixture above as a real asyncio subprocess with
    its own process group, exactly like `_spawn_background_process` now
    does for real K8s-mode background processes."""
    script = _fork_and_sleep_script(marker_file, inner_command)
    return await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )


async def _wait_for_marker_pid(marker_file, attempts: int = 200) -> int:
    """Poll until `marker_file` contains a PID written by the forked
    grandchild, then return it. Deterministic instead of a fixed sleep."""
    for _ in range(attempts):
        if marker_file.exists() and marker_file.read_text().strip():
            return int(marker_file.read_text().strip())
        await asyncio.sleep(0.05)
    raise AssertionError(f"grandchild never wrote its PID to {marker_file}")


def _is_alive(pid: int) -> bool:
    """True if `pid` is a real, still-scheduled process.

    A zombie (exited, not yet reaped by whatever it ended up reparented to)
    counts as NOT alive here: its signal was already delivered and fully
    processed by the kernel -- it's just an exit-status placeholder in the
    process table until *something* calls wait() on it, and once a process
    has been reparented away from us (as every process this file kills
    after its original parent died has been), we are not that process's
    parent and cannot reap it ourselves; that's the new parent's (in real
    K8s, the pod's pause container's) job, not the sidecar's. Only matters
    in environments with a minimal, non-reaping init (e.g. some bare test
    containers whose PID 1 is a plain `sleep infinity`); real CI runners
    and K8s's own pause container both reap properly and this branch is
    never exercised there.
    """
    status_path = f"/proc/{pid}/status"
    if os.path.exists(status_path):
        try:
            with open(status_path) as f:
                for line in f:
                    if line.startswith("State:"):
                        return "Z" not in line
        except OSError:
            pass
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


async def _reap(proc) -> None:
    try:
        await asyncio.wait_for(proc.wait(), timeout=5)
    except asyncio.TimeoutError:
        pass


# ---------------------------------------------------------------------------
# _spawn_background_process: process-group + marker-env wiring
# ---------------------------------------------------------------------------


async def test_spawn_background_process_k8s_mode_uses_new_session_and_marker(monkeypatch):
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "k8s")
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 999999)

    captured = {}
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def _spy(*args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs
        # What actually runs doesn't matter -- this test is about the
        # arguments _spawn_background_process passes, not nsenter's
        # behavior (covered by the process-group tests below).
        return await real_create_subprocess_exec(
            "true",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    monkeypatch.setattr(sidecar_main.asyncio, "create_subprocess_exec", _spy)

    proc = await sidecar_main._spawn_background_process("echo hi")
    await proc.wait()

    assert captured["kwargs"]["start_new_session"] is True
    assert (
        captured["kwargs"]["env"][sidecar_main.BACKGROUND_PROCESS_MARKER_ENV]
        == sidecar_main.BACKGROUND_PROCESS_MARKER_VALUE
    )
    assert captured["args"][0] in ("nsenter", "unshare")


async def test_spawn_background_process_compose_mode_also_gets_marker(monkeypatch):
    """Compose mode used to skip the marker because docker exec's process
    wasn't visible in this sidecar's own /proc. Since deploy/docker-compose.yml
    now shares a PID namespace with the sandbox container (`pid:
    "container:sandbox"`) and exec goes through the same nsenter path as K8s
    mode (see get_sandbox_pid's docstring), the marker is visible and set
    here too -- this is the inverse of what this test used to assert."""
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "compose")
    monkeypatch.setattr(sidecar_main, "get_sandbox_pid", lambda: 999999)

    captured = {}
    real_create_subprocess_exec = asyncio.create_subprocess_exec

    async def _spy(*args, **kwargs):
        captured["kwargs"] = kwargs
        return await real_create_subprocess_exec(
            "true",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )

    monkeypatch.setattr(sidecar_main.asyncio, "create_subprocess_exec", _spy)

    proc = await sidecar_main._spawn_background_process("echo hi")
    await proc.wait()

    assert (
        captured["kwargs"]["env"][sidecar_main.BACKGROUND_PROCESS_MARKER_ENV]
        == sidecar_main.BACKGROUND_PROCESS_MARKER_VALUE
    )


# ---------------------------------------------------------------------------
# _signal_process_group: the actual fix for "killing the tracked PID doesn't
# kill the real sandboxed command"
# ---------------------------------------------------------------------------


async def test_direct_kill_of_tracked_pid_leaves_forked_grandchild_running(tmp_path):
    """Documents the bug this issue found: killing only the process the
    sidecar tracks (nsenter's own PID, in K8s mode) does NOT terminate the
    real sandboxed command nsenter wraps, because nsenter forks internally
    to enter the target PID namespace. Reproduced here without nsenter using
    a real fork() with the exact same shape (one internal fork, no setpgid
    on the result)."""
    marker_file = tmp_path / "grandchild.pid"
    proc = await _spawn_fork_tree(marker_file)
    grandchild_pid = await _wait_for_marker_pid(marker_file)
    assert grandchild_pid != proc.pid, "sanity: the fork must produce a distinct OS process"

    try:
        os.kill(proc.pid, signal.SIGKILL)
        await asyncio.sleep(0.3)

        assert _is_alive(grandchild_pid), (
            "expected the grandchild to survive a direct kill of only the "
            "tracked PID -- if this now fails, this test's fork-tree "
            "fixture no longer matches nsenter's verified shape and the "
            "rest of this file's assumptions need re-checking"
        )
    finally:
        if _is_alive(grandchild_pid):
            os.kill(grandchild_pid, signal.SIGKILL)
        await _reap(proc)


async def test_signal_process_group_kills_the_whole_tree(tmp_path):
    """The actual fix: signalling the whole process group (what
    `_stop_process`/`_kill_all_processes`/`_process_watchdog` now do via
    `_signal_process_group`) reaches the grandchild too."""
    marker_file = tmp_path / "grandchild.pid"
    proc = await _spawn_fork_tree(marker_file)
    grandchild_pid = await _wait_for_marker_pid(marker_file)

    sidecar_main._signal_process_group(proc, signal.SIGKILL)

    for _ in range(200):
        if not _is_alive(grandchild_pid):
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("grandchild survived a process-group kill")

    await _reap(proc)


async def test_signal_process_group_never_kills_callers_own_group():
    """Regression test for a real self-inflicted-DoS bug found while
    building this fix: a process spawned WITHOUT its own session (as this
    repo's `_fake_spawn` test double in test_sidecar_process_sessions.py
    does) shares the CALLING process's own process group. Before the
    same-group guard was added, `_signal_process_group` would `killpg` that
    shared group -- which reproduced immediately as pytest itself being
    SIGKILLed mid-test-run. This test would kill its own test runner if the
    guard regresses."""
    proc = await asyncio.create_subprocess_shell(
        "sleep 30",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    assert os.getpgid(proc.pid) == os.getpgid(0), "sanity: shares our own group"

    sidecar_main._signal_process_group(proc, signal.SIGKILL)

    await asyncio.wait_for(proc.wait(), timeout=5)
    assert proc.returncode is not None
    # If we reach this line at all, the test process itself survived --
    # that's the actual assertion.


# ---------------------------------------------------------------------------
# _sweep_orphaned_background_processes: startup-time reaper for whatever
# survives a hard crash despite the fix above
# ---------------------------------------------------------------------------


@requires_proc
async def test_sweep_reaps_marked_orphan_left_over_from_a_previous_incarnation(tmp_path, monkeypatch):
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "k8s")
    monkeypatch.setattr(sidecar_main, "SANDBOX_PROCESS_STARTUP_SWEEP_ENABLED", True)

    marker_file = tmp_path / "orphan.pid"
    env = dict(os.environ)
    env[sidecar_main.BACKGROUND_PROCESS_MARKER_ENV] = sidecar_main.BACKGROUND_PROCESS_MARKER_VALUE
    proc = await _spawn_fork_tree(marker_file, env=env)
    orphan_pid = await _wait_for_marker_pid(marker_file)
    # Deliberately NOT tracked in _process_registry -- this represents
    # exactly what remains after a hard sidecar crash: the in-memory
    # registry that would have known about this process is gone, but the OS
    # process (and its marker env, which is immutable kernel-tracked state
    # from exec time, not a file the sandboxed command could tamper with)
    # is still there.

    reaped = sidecar_main._sweep_orphaned_background_processes()
    assert reaped >= 1

    for _ in range(200):
        if not _is_alive(orphan_pid):
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("sweep did not reap the marked orphan")

    await _reap(proc)


@requires_proc
async def test_sweep_leaves_unmarked_process_untouched(monkeypatch):
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "k8s")
    monkeypatch.setattr(sidecar_main, "SANDBOX_PROCESS_STARTUP_SWEEP_ENABLED", True)

    proc = await asyncio.create_subprocess_shell(
        "sleep 30",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    try:
        sidecar_main._sweep_orphaned_background_processes()
        await asyncio.sleep(0.2)
        assert proc.returncode is None
        assert _is_alive(proc.pid)
    finally:
        proc.kill()
        await proc.wait()


@requires_proc
async def test_sweep_also_reaps_marked_orphans_in_compose_mode(tmp_path, monkeypatch):
    """Compose mode used to skip the startup sweep entirely, because docker
    exec's process wasn't visible in this sidecar's own /proc. Since
    deploy/docker-compose.yml now shares a PID namespace with the sandbox
    container and exec goes through the same nsenter path as K8s mode, the
    same reaping logic applies in both modes -- this is the inverse of what
    this test used to assert (that compose mode was always a no-op sweep)."""
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "compose")
    monkeypatch.setattr(sidecar_main, "SANDBOX_PROCESS_STARTUP_SWEEP_ENABLED", True)

    marker_file = tmp_path / "orphan.pid"
    env = dict(os.environ)
    env[sidecar_main.BACKGROUND_PROCESS_MARKER_ENV] = sidecar_main.BACKGROUND_PROCESS_MARKER_VALUE
    proc = await _spawn_fork_tree(marker_file, env=env)
    orphan_pid = await _wait_for_marker_pid(marker_file)

    reaped = sidecar_main._sweep_orphaned_background_processes()
    assert reaped >= 1

    for _ in range(200):
        if not _is_alive(orphan_pid):
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError("sweep did not reap the marked orphan in compose mode")

    await _reap(proc)


def test_sweep_respects_disabled_flag(monkeypatch):
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "k8s")
    monkeypatch.setattr(sidecar_main, "SANDBOX_PROCESS_STARTUP_SWEEP_ENABLED", False)
    assert sidecar_main._sweep_orphaned_background_processes() == 0


# ---------------------------------------------------------------------------
# End-to-end: the exact scenario in the issue's acceptance criteria --
# start a background process through the real API, simulate a sidecar
# restart, and check whether the OS process was reaped.
# ---------------------------------------------------------------------------


@requires_proc
async def test_restart_simulation_orphan_reaped_by_fresh_incarnation_sweep(tmp_path, monkeypatch):
    """Starts a background process through the real `/process/start` route,
    then simulates a hard sidecar crash (SIGKILL to the tracked process,
    bypassing `_stop_process`/`_kill_all_processes`/`shutdown_event`
    entirely -- nothing in that graceful path runs on a real OOM-kill
    either), then simulates the sidecar restarting (a fresh process has an
    empty registry -- reproduced here by clearing it directly rather than
    actually restarting the test process) and confirms the startup sweep
    reaps the orphan a fresh incarnation would otherwise never know about.
    """
    monkeypatch.setattr(sidecar_main, "SIDECAR_AUTH_TOKEN", AUTH_TOKEN)
    monkeypatch.setattr(sidecar_main, "RUNTIME_MODE", "k8s")
    monkeypatch.setattr(sidecar_main, "SANDBOX_PROCESS_STARTUP_SWEEP_ENABLED", True)

    marker_file = tmp_path / "grandchild.pid"

    async def _fake_spawn_like_nsenter(command: str, *, expose_network: bool = False):
        # Mirrors nsenter -p's real, verified shape (one internal fork, no
        # setpgid on the result) via a real os.fork(), not shell
        # backgrounding -- see this module's docstring for why.
        env = dict(os.environ)
        env[sidecar_main.BACKGROUND_PROCESS_MARKER_ENV] = sidecar_main.BACKGROUND_PROCESS_MARKER_VALUE
        return await _spawn_fork_tree(marker_file, env=env, inner_command=command)

    monkeypatch.setattr(sidecar_main, "_spawn_background_process", _fake_spawn_like_nsenter)
    client = _client()

    start = client.post(
        "/process/start",
        json={"command": "sleep 60", "max_runtime_seconds": 120},
        headers=_headers(),
    )
    assert start.status_code == 201
    process_id = start.json()["process_id"]
    handle = sidecar_main._process_registry[process_id]

    grandchild_pid = await _wait_for_marker_pid(marker_file)

    # --- simulate a hard sidecar crash ---
    os.kill(handle.proc.pid, signal.SIGKILL)
    await asyncio.sleep(0.3)
    assert _is_alive(grandchild_pid), "the orphan should still be running at this point -- that's the leak"

    # --- simulate the sidecar restarting: fresh, empty in-memory state ---
    sidecar_main._process_registry.clear()
    sidecar_main._exposed_ports.clear()

    reaped = sidecar_main._sweep_orphaned_background_processes()
    assert reaped >= 1

    for _ in range(200):
        if not _is_alive(grandchild_pid):
            break
        await asyncio.sleep(0.05)
    else:
        raise AssertionError(
            "the orphaned background process survived the simulated sidecar "
            "restart -- the sweep did not reap it"
        )
