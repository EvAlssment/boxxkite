#!/usr/bin/env python3
"""Multi-metric latency benchmark against a real, running boxkite control-plane.

A superset of scripts/benchmark_warm_pool.py: instead of only session-create
latency, it measures the operations an agent actually performs end-to-end, so
the numbers can be compared across a spread of dimensions rather than a single
one. Every measurement is wall-clock, client-side, from just before the request
to just after the response — the same "time to a usable result" boundary the
warm-pool script documents.

Usage (repo root, after `pip install -e .`):

    BOXKITE_BASE_URL=https://your-control-plane... \\
    BOXKITE_API_KEY=bxk_live_... \\
    python scripts/benchmark_suite.py --create-samples 5 --op-samples 10 \\
        --json-out benchmark_result.json

Metrics
-------
- create_warm_ms          create with a warm pod likely claimable (back-to-back)
- create_cold_ms          create after a gap, biased toward a cold build
- destroy_ms              session teardown (DELETE)
- exec_echo_ms            trivial bash exec round-trip (sidecar exec overhead)
- exec_python_ms          `python3 -c` exec round-trip (interpreter spin-up)
- file_write_ms           create a small file (file_create)
- file_read_ms            read it back (file view)
- process_first_output_ms start a background process, time to its first output
- concurrent_create_2_ms  wall-clock to create 2 sandboxes in parallel

Per-op metrics reuse one long-lived session so they measure the operation, not
session creation. Everything created is destroyed in a finally block, plus a
best-effort sweep at start and end.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import statistics
import sys
import time

import httpx

DEFAULT_TIMEOUT_SECONDS = 120.0


def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = q * (len(ordered) - 1)
    lo = int(pos)
    frac = pos - lo
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * frac


def summarize(name: str, samples_ms: list[float]) -> dict:
    if not samples_ms:
        return {"name": name, "n": 0}
    return {
        "name": name,
        "n": len(samples_ms),
        "min_ms": round(min(samples_ms), 1),
        "p50_ms": round(_pct(samples_ms, 0.50), 1),
        "p90_ms": round(_pct(samples_ms, 0.90), 1),
        "p95_ms": round(_pct(samples_ms, 0.95), 1),
        "max_ms": round(max(samples_ms), 1),
        "raw_ms": [round(v, 1) for v in samples_ms],
    }


class Bench:
    def __init__(self, client: httpx.Client):
        self.c = client

    def _timed(self, method: str, path: str, **kw) -> tuple[httpx.Response, float]:
        t0 = time.perf_counter()
        resp = self.c.request(method, path, **kw)
        return resp, (time.perf_counter() - t0) * 1000.0

    def create(self, label: str) -> tuple[str, float]:
        resp, ms = self._timed("POST", "/v1/sandboxes", json={"label": label})
        resp.raise_for_status()
        return resp.json()["id"], ms

    def destroy(self, session_id: str) -> float:
        _, ms = self._timed("DELETE", f"/v1/sandboxes/{session_id}")
        return ms

    def sweep(self) -> None:
        try:
            resp = self.c.get("/v1/sandboxes", params={"active_only": "true"})
            if resp.status_code < 400:
                for row in resp.json():
                    try:
                        self.c.delete(f"/v1/sandboxes/{row['id']}")
                    except httpx.HTTPError:
                        pass
        except httpx.HTTPError as exc:
            print(f"WARNING: sweep failed: {exc}", file=sys.stderr)

    # ── create / destroy series ──────────────────────────────────────────
    def create_series(self, n: int, *, gap_s: float, tag: str) -> list[float]:
        out: list[float] = []
        for i in range(n):
            if gap_s and i > 0:
                time.sleep(gap_s)
            sid, ms = self.create(f"bench-{tag}-{i}")
            out.append(ms)
            self.destroy(sid)
        return out

    def destroy_series(self, n: int) -> list[float]:
        out: list[float] = []
        for i in range(n):
            sid, _ = self.create(f"bench-destroy-{i}")
            out.append(self.destroy(sid))
        return out

    # ── per-op series (reuse one session) ────────────────────────────────
    def exec_series(self, sid: str, command: str, n: int) -> list[float]:
        out: list[float] = []
        for _ in range(n):
            resp, ms = self._timed(
                "POST", f"/v1/sandboxes/{sid}/exec", json={"command": command}
            )
            resp.raise_for_status()
            out.append(ms)
        return out

    def file_write_series(self, sid: str, n: int) -> list[float]:
        out: list[float] = []
        for i in range(n):
            resp, ms = self._timed(
                "POST",
                f"/v1/sandboxes/{sid}/files",
                json={"path": f"/workspace/bench_{i}.txt", "content": "boxkite benchmark line\n" * 5},
            )
            resp.raise_for_status()
            out.append(ms)
        return out

    def file_read_series(self, sid: str, n: int) -> list[float]:
        out: list[float] = []
        for i in range(n):
            resp, ms = self._timed(
                "POST", f"/v1/sandboxes/{sid}/files/view", json={"path": f"/workspace/bench_{i}.txt"}
            )
            resp.raise_for_status()
            out.append(ms)
        return out

    def process_first_output_series(self, sid: str, n: int) -> list[float]:
        out: list[float] = []
        for i in range(n):
            t0 = time.perf_counter()
            resp = self.c.post(
                f"/v1/sandboxes/{sid}/processes",
                json={"command": "echo boxkite-proc", "max_runtime_seconds": 60},
            )
            resp.raise_for_status()
            pid = resp.json()["process_id"]
            offset = 0
            while True:
                poll = self.c.get(
                    f"/v1/sandboxes/{sid}/processes/{pid}/output", params={"since_offset": offset}
                )
                poll.raise_for_status()
                body = poll.json()
                if body.get("stdout_chunk"):
                    break
                if body.get("status") != "running":
                    break
                if time.perf_counter() - t0 > 30:
                    break
            out.append((time.perf_counter() - t0) * 1000.0)
        return out

    def concurrent_create_series(self, n_rounds: int, parallelism: int) -> list[float]:
        out: list[float] = []
        for r in range(n_rounds):
            created: list[str] = []
            t0 = time.perf_counter()
            with concurrent.futures.ThreadPoolExecutor(max_workers=parallelism) as ex:
                futs = [ex.submit(self.create, f"bench-conc-{r}-{k}") for k in range(parallelism)]
                for f in concurrent.futures.as_completed(futs):
                    try:
                        created.append(f.result()[0])
                    except Exception as exc:  # noqa: BLE001
                        print(f"WARNING: concurrent create failed: {exc}", file=sys.stderr)
            out.append((time.perf_counter() - t0) * 1000.0)
            for sid in created:
                self.destroy(sid)
        return out


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--create-samples", type=int, default=5)
    p.add_argument("--op-samples", type=int, default=10)
    p.add_argument("--cold-gap-seconds", type=float, default=20.0)
    p.add_argument("--concurrent-rounds", type=int, default=3)
    p.add_argument("--concurrency", type=int, default=2)
    p.add_argument("--json-out", default=None)
    args = p.parse_args()

    base_url = os.environ.get("BOXKITE_BASE_URL", "").rstrip("/")
    api_key = os.environ.get("BOXKITE_API_KEY", "")
    if not base_url or not api_key:
        print("BOXKITE_BASE_URL and BOXKITE_API_KEY must both be set.", file=sys.stderr)
        return 2

    results: dict = {"base_url": base_url, "metrics": {}}
    with httpx.Client(
        base_url=base_url, headers={"Authorization": f"Bearer {api_key}"}, timeout=DEFAULT_TIMEOUT_SECONDS
    ) as client:
        b = Bench(client)
        b.sweep()
        try:
            def record(name, samples):
                s = summarize(name, samples)
                results["metrics"][name] = s
                print(json.dumps(s))

            print("== create_warm ==")
            record("create_warm_ms", b.create_series(args.create_samples, gap_s=0, tag="warm"))
            print("== create_cold ==")
            record("create_cold_ms", b.create_series(args.create_samples, gap_s=args.cold_gap_seconds, tag="cold"))
            print("== destroy ==")
            record("destroy_ms", b.destroy_series(args.create_samples))

            print("== per-op (shared session) ==")
            sid, _ = b.create("bench-ops")
            try:
                record("exec_echo_ms", b.exec_series(sid, "echo boxkite", args.op_samples))
                record("exec_python_ms", b.exec_series(sid, "python3 -c 'print(2+2)'", args.op_samples))
                record("file_write_ms", b.file_write_series(sid, args.op_samples))
                record("file_read_ms", b.file_read_series(sid, args.op_samples))
                record("process_first_output_ms", b.process_first_output_series(sid, args.op_samples))
            finally:
                b.destroy(sid)

            print("== concurrent create ==")
            record(
                "concurrent_create_2_ms",
                b.concurrent_create_series(args.concurrent_rounds, args.concurrency),
            )
        finally:
            b.sweep()

    print("\n== full result ==")
    print(json.dumps(results, indent=2))
    if args.json_out:
        with open(args.json_out, "w") as f:
            json.dump(results, f, indent=2)
        print(f"\nWrote {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
