#!/usr/bin/env python3
"""Snapshot /cgroup-stats before/after load and compute CFS throttling deltas."""

from __future__ import annotations

import argparse
import http.client
import json
import time
from pathlib import Path
from typing import Any


def fetch_cgroup_stats(host: str, port: int) -> dict[str, Any]:
    conn = http.client.HTTPConnection(host, port, timeout=5)
    try:
        conn.request("GET", "/cgroup-stats")
        resp = conn.getresponse()
        body = resp.read().decode("utf-8")
        if resp.status != 200:
            raise RuntimeError(f"GET /cgroup-stats returned HTTP {resp.status}: {body}")
        return dict(json.loads(body))
    finally:
        conn.close()


def cmd_snapshot(args: argparse.Namespace) -> None:
    stats = fetch_cgroup_stats(args.host, args.port)
    snapshot = {
        "captured_at_epoch": time.time(),
        "stats": stats,
    }
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(snapshot, indent=2) + "\n", encoding="utf-8")


def cmd_report(args: argparse.Namespace) -> None:
    before_doc = json.loads(Path(args.before).read_text(encoding="utf-8"))
    after_epoch = time.time()
    after_stats = fetch_cgroup_stats(args.host, args.port)
    before_stats = before_doc["stats"]

    wall_seconds = max(after_epoch - float(before_doc["captured_at_epoch"]), 1e-6)
    delta_nr_periods = int(after_stats["nr_periods"]) - int(before_stats["nr_periods"])
    delta_nr_throttled = (
        int(after_stats["nr_throttled"]) - int(before_stats["nr_throttled"])
    )
    delta_throttled_usec = (
        int(after_stats["throttled_usec"]) - int(before_stats["throttled_usec"])
    )
    delta_usage_usec = int(after_stats["usage_usec"]) - int(before_stats["usage_usec"])

    throttled_ratio = (
        delta_nr_throttled / delta_nr_periods if delta_nr_periods > 0 else 0.0
    )

    quota_usec = after_stats.get("cpu_quota_usec")
    period_usec = after_stats.get("cpu_period_usec")
    cpu_limit_cores: float | None = None
    avg_cpu_util_of_limit: float | None = None
    if quota_usec and period_usec and quota_usec > 0 and period_usec > 0:
        cpu_limit_cores = quota_usec / period_usec
        avg_cpu_util_of_limit = delta_usage_usec / (
            wall_seconds * cpu_limit_cores * 1_000_000.0
        )

    summary = {
        "profile": after_stats.get("profile", "unknown"),
        "cpu_max": after_stats.get("cpu_max", "unknown"),
        "cpu_limit_cores": round(cpu_limit_cores, 4) if cpu_limit_cores else None,
        "wall_seconds": round(wall_seconds, 3),
        "delta_usage_usec": delta_usage_usec,
        "delta_nr_periods": delta_nr_periods,
        "delta_nr_throttled": delta_nr_throttled,
        "throttled_period_ratio": round(throttled_ratio, 6),
        "throttled_period_pct": round(throttled_ratio * 100.0, 2),
        "delta_throttled_usec": delta_throttled_usec,
        "delta_throttled_seconds": round(delta_throttled_usec / 1_000_000.0, 6),
        "avg_cpu_util_of_limit": (
            round(avg_cpu_util_of_limit, 6)
            if avg_cpu_util_of_limit is not None
            else None
        ),
        "avg_cpu_util_of_limit_pct": (
            round(avg_cpu_util_of_limit * 100.0, 2)
            if avg_cpu_util_of_limit is not None
            else None
        ),
    }

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print("--- cgroup CPU delta summary ---")
    print(json.dumps(summary, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Lab 01 cgroup CPU delta utility.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snap = subparsers.add_parser("snapshot", help="Capture pre-load /cgroup-stats.")
    snap.add_argument("--host", default="127.0.0.1")
    snap.add_argument("--port", type=int, default=8081)
    snap.add_argument("--out", required=True)
    snap.set_defaults(func=cmd_snapshot)

    rep = subparsers.add_parser("report", help="Compute post-load cgroup deltas.")
    rep.add_argument("--before", required=True)
    rep.add_argument("--host", default="127.0.0.1")
    rep.add_argument("--port", type=int, default=8081)
    rep.add_argument("--out", default=None)
    rep.set_defaults(func=cmd_report)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
