#!/usr/bin/env python3
"""Benchmark: time-to-usable-sandbox against a real, running boxkite
control-plane deployment.

Same live-deployment pattern as `mcp-server/tests/live_smoke.py` and
`examples/hosted_control_plane/hosted_flow.py`: this is a manual, non-CI
script that talks HTTP directly to a real control-plane instance (no mocks,
no fakes). Run it with:

    BOXKITE_BASE_URL=https://your-control-plane.example.com \\
    BOXKITE_API_KEY=bxk_live_... \\
    python scripts/benchmark_warm_pool.py --samples 5

Install first (repo root): `pip install -e .` (this script only needs the
`httpx` dependency that already ships with the `boxkite` package).

What this measures
-------------------
Two named latency series, both wall-clock time from the moment this script
issues `POST /v1/sandboxes` to the moment the control-plane responds 201
with a session the caller can immediately `exec` against (the control-plane
already waits for the sidecar's `/configure` call to succeed before
returning -- see `src/boxkite/manager.py::_create_k8s_session` -- so a 201
response IS "usable", not just "pod object created"):

  - ``claim_latency_ms``: latency of a create call when a warm pod *may* be
    available for `SandboxManager._claim_warm_pod_via_k8s` to claim instead
    of building a new one from scratch.
  - ``cold_start_latency_ms``: latency of a create call with no warm pod to
    claim, i.e. a full pod build from `SandboxManager._create_pod`.

The control-plane's public HTTP API has no request field to force one path
or the other -- `POST /v1/sandboxes` always tries the warm-pool claim first
and silently falls back to cold-create (see `sandboxes.py` -> `UsagePolicy`
-> `SandboxManager.create_session`). So this script approximates the two
conditions the only way an external, black-box HTTP caller can:

  - "claim" samples: create session N, destroy it, *immediately* create
    session N+1 -- giving any warm-pool replenisher/recycler the best
    realistic chance to have a claimable pod ready.
  - "cold" samples: sleep ``--cold-gap-seconds`` (default 15s) between
    destroy and the next create, and are taken as a separate, later batch --
    reducing (not eliminating -- see exclusions below) the chance that a
    pod recycled from the just-destroyed session is still claimable.

IMPORTANT deployment-specific caveat, verified by reading the source of
*this* repo before running this script: `control-plane/src/control_plane/main.py`'s
`lifespan()` never constructs or starts a `WarmPoolManager` (there is no
call to `boxkite.get_warm_pool()` or `WarmPoolManager()` anywhere in
`control-plane/`), and no `deploy/` manifest runs one as a separate process
either. `SandboxManager._claim_warm_pod_via_k8s` (the method every create
call actually invokes) queries K8s directly by pod label
(`pool=warm,status=warm`) rather than depending on any in-memory
`WarmPoolManager` state, but with no replenisher process running anywhere,
nothing ever *applies* those labels to a pod in the first place. So against
any control-plane deployment that hasn't separately wired up and started a
`WarmPoolManager` background loop, `claim_latency_ms` and
`cold_start_latency_ms` are expected to converge to the same underlying
cold-create code path -- see docs/BENCHMARKS.md for the actual measured
numbers and what they do (and do not) show.

Deliberately excluded, and why
-------------------------------
- **Network variance between this script's machine and the control-plane.**
  We report min/median/max of repeated samples specifically so that one-off
  network jitter (DNS, TLS handshake, a slow home/office uplink) shows up as
  spread rather than being mistaken for a warm/cold difference, but we do
  not attempt to isolate or subtract client-side network RTT -- that would
  require a benchmark harness colocated with the control-plane, which this
  script is not.
- **First-run/cold-cache container image pulls.** If the underlying K8s
  node autoscales and schedules the sandbox/sidecar pod onto a node that has
  never pulled `SANDBOX_IMAGE`/`SIDECAR_IMAGE` before, image pull time can
  dominate a single sample by many seconds -- and that cost is about node
  bin-packing/cluster autoscaler behavior at the time of the run, not about
  whether a pod was claimed warm or built cold. We don't detect, retry
  around, or exclude such outlier samples automatically; we report raw
  min/median/max and call out anything that looks image-pull-dominated in
  the results doc instead of quietly discarding it.
- **Concurrent multi-tenant load.** Samples are taken serially, one sandbox
  session at a time (this also respects the hosted control-plane's
  concurrent-sandbox fair-use cap -- see `BOXKITE_MAX_CONCURRENT_SANDBOXES`
  in `control-plane/src/control_plane/config.py`). This does not measure
  contention effects under many simultaneous callers.
- **Any competitor's (e.g. GKE Agent Sandbox) own cold-start number.** This
  script only exercises boxkite's control-plane; it cannot measure a
  third-party managed service it has no credentials for. Any comparison to
  a competitor's published number is a citation, not something this script
  produces -- see docs/BENCHMARKS.md.

Cleanup
-------
Every sandbox session this script creates is destroyed in a `finally`
block, and the script also does a best-effort sweep of any sessions still
active under this account at the end (in case a run is interrupted).
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field

import httpx

DEFAULT_SAMPLES = 5
DEFAULT_COLD_GAP_SECONDS = 15.0
DEFAULT_TIMEOUT_SECONDS = 120.0


@dataclass
class LatencySeries:
    name: str
    samples_ms: list[float] = field(default_factory=list)

    def add(self, latency_ms: float) -> None:
        self.samples_ms.append(latency_ms)

    def summary(self) -> dict:
        if not self.samples_ms:
            return {"name": self.name, "n": 0, "min_ms": None, "median_ms": None, "max_ms": None}
        return {
            "name": self.name,
            "n": len(self.samples_ms),
            "min_ms": round(min(self.samples_ms), 1),
            "median_ms": round(statistics.median(self.samples_ms), 1),
            "max_ms": round(max(self.samples_ms), 1),
            "raw_ms": [round(v, 1) for v in self.samples_ms],
        }


class BenchmarkError(RuntimeError):
    pass


def _raise_for_status(resp: httpx.Response, step: str) -> None:
    if resp.status_code >= 400:
        raise BenchmarkError(f"[{step}] HTTP {resp.status_code}: {resp.text}")


def create_sandbox(client: httpx.Client, *, label: str) -> tuple[str, float]:
    """POST /v1/sandboxes and return (session_id, latency_ms).

    Latency is measured from just before the request is sent to just after
    the response is fully received -- this is the same "usable" boundary
    described in the module docstring, not merely "pod object accepted".
    """
    t0 = time.perf_counter()
    resp = client.post("/v1/sandboxes", json={"label": label})
    latency_ms = (time.perf_counter() - t0) * 1000.0
    _raise_for_status(resp, "create sandbox")
    session_id = resp.json()["id"]
    return session_id, latency_ms


def destroy_sandbox(client: httpx.Client, session_id: str) -> None:
    resp = client.delete(f"/v1/sandboxes/{session_id}")
    if resp.status_code >= 400 and resp.status_code != 404:
        print(f"WARNING: failed to destroy session {session_id}: HTTP {resp.status_code}: {resp.text}", file=sys.stderr)


def sweep_active_sandboxes(client: httpx.Client) -> None:
    """Best-effort cleanup of any sessions left active on this account,
    e.g. after an interrupted run. Never raises -- cleanup should not mask
    the actual benchmark result or crash on a network blip."""
    try:
        resp = client.get("/v1/sandboxes", params={"active_only": "true"})
        if resp.status_code >= 400:
            return
        for row in resp.json():
            destroy_sandbox(client, row["id"])
    except httpx.HTTPError as exc:
        print(f"WARNING: cleanup sweep failed: {exc}", file=sys.stderr)


def run_claim_samples(client: httpx.Client, *, n: int, label_prefix: str) -> LatencySeries:
    """Best-case-for-warm-pool samples: destroy then immediately create
    again, back to back, giving any warm-pool replenisher the shortest
    possible window to have refilled a claimable pod."""
    series = LatencySeries(name="claim_latency_ms")
    for i in range(n):
        session_id, latency_ms = create_sandbox(client, label=f"{label_prefix}-claim-{i}")
        try:
            series.add(latency_ms)
        finally:
            destroy_sandbox(client, session_id)
    return series


def run_cold_samples(client: httpx.Client, *, n: int, label_prefix: str, cold_gap_seconds: float) -> LatencySeries:
    """Worst-case-for-warm-pool samples: sleep between destroy and the next
    create so any just-recycled pod has time to leave the claimable set
    before the next attempt."""
    series = LatencySeries(name="cold_start_latency_ms")
    for i in range(n):
        if i > 0:
            time.sleep(cold_gap_seconds)
        session_id, latency_ms = create_sandbox(client, label=f"{label_prefix}-cold-{i}")
        try:
            series.add(latency_ms)
        finally:
            destroy_sandbox(client, session_id)
    return series


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--samples", type=int, default=DEFAULT_SAMPLES, help=f"Samples per series (default {DEFAULT_SAMPLES}).")
    parser.add_argument(
        "--cold-gap-seconds",
        type=float,
        default=DEFAULT_COLD_GAP_SECONDS,
        help=f"Seconds to sleep between cold-series destroy and next create (default {DEFAULT_COLD_GAP_SECONDS}).",
    )
    parser.add_argument("--label-prefix", default="benchmark-warm-pool", help="Sandbox session label prefix.")
    parser.add_argument("--json-out", default=None, help="Optional path to write the full JSON result to.")
    args = parser.parse_args()

    base_url = os.environ.get("BOXKITE_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("BOXKITE_API_KEY", "")
    if not base_url or not api_key:
        print("BOXKITE_BASE_URL and BOXKITE_API_KEY must both be set.", file=sys.stderr)
        return 2

    with httpx.Client(
        base_url=base_url,
        headers={"Authorization": f"Bearer {api_key}"},
        timeout=DEFAULT_TIMEOUT_SECONDS,
    ) as client:
        # Clean slate: destroy anything left over from a prior interrupted run
        # before this run's own measurements start.
        sweep_active_sandboxes(client)

        try:
            print(f"== claim series ({args.samples} samples, back-to-back) ==")
            claim_series = run_claim_samples(client, n=args.samples, label_prefix=args.label_prefix)
            print(json.dumps(claim_series.summary(), indent=2))

            print(f"\n== cold series ({args.samples} samples, {args.cold_gap_seconds}s gap) ==")
            cold_series = run_cold_samples(
                client, n=args.samples, label_prefix=args.label_prefix, cold_gap_seconds=args.cold_gap_seconds
            )
            print(json.dumps(cold_series.summary(), indent=2))
        finally:
            # Cleanup pass in case any create succeeded but a later step in
            # this script raised before its own destroy ran.
            sweep_active_sandboxes(client)

        result = {
            "base_url": base_url,
            "samples_per_series": args.samples,
            "cold_gap_seconds": args.cold_gap_seconds,
            "claim_latency_ms": claim_series.summary(),
            "cold_start_latency_ms": cold_series.summary(),
        }
        print("\n== full result ==")
        print(json.dumps(result, indent=2))

        if args.json_out:
            with open(args.json_out, "w") as f:
                json.dump(result, f, indent=2)
            print(f"\nWrote {args.json_out}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
