#!/usr/bin/env python3
"""Summarize Lab 04 zero-downtime rollout / drain experiment artifacts."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        text = line.strip()
        if text:
            rows.append(json.loads(text))
    return rows


def _window_stats(
    rows: list[dict[str, Any]],
    start_s: int,
    end_s: int,
) -> dict[str, Any]:
    sliced = [r for r in rows if start_s <= int(r["sec"]) < end_s]
    total = sum(int(r["total"]) for r in sliced)
    ok = sum(int(r["ok"]) for r in sliced)
    http_5xx = sum(int(r["http_5xx"]) for r in sliced)
    http_other = sum(int(r["http_other"]) for r in sliced)
    conn_refused = sum(int(r["conn_refused"]) for r in sliced)
    conn_reset = sum(int(r["conn_reset"]) for r in sliced)
    timeout = sum(int(r["timeout"]) for r in sliced)
    other_err = sum(int(r["other_err"]) for r in sliced)
    non_ok = total - ok
    success_rate = round((ok / total) * 100.0, 3) if total > 0 else 0.0
    return {
        "window_s": f"{start_s}-{end_s}",
        "total": total,
        "ok": ok,
        "non_ok": non_ok,
        "http_5xx": http_5xx,
        "http_other": http_other,
        "conn_refused": conn_refused,
        "conn_reset": conn_reset,
        "timeout": timeout,
        "other_err": other_err,
        "success_rate_pct": success_rate,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize Lab 04 rollout run.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--baseline-s", type=int, default=15)
    parser.add_argument("--duration-s", type=int, default=75)
    parser.add_argument("--rollout-elapsed-s", type=int, default=0)
    parser.add_argument("--client-jsonl", required=True)
    parser.add_argument("--timeline-jsonl", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    client_rows = _load_jsonl(Path(args.client_jsonl))
    timeline = _load_jsonl(Path(args.timeline_jsonl))

    failed_seconds = [
        {
            "sec": int(r["sec"]),
            "ok": int(r["ok"]),
            "http_5xx": int(r["http_5xx"]),
            "conn_refused": int(r["conn_refused"]),
            "conn_reset": int(r["conn_reset"]),
            "timeout": int(r["timeout"]),
            "other_err": int(r["other_err"]),
        }
        for r in client_rows
        if int(r["ok"]) < int(r["total"])
    ]

    ready_ep_values = [int(t["ready_endpoints"]) for t in timeline]
    min_ready_ep = min(ready_ep_values) if ready_ep_values else None
    max_ready_ep = max(ready_ep_values) if ready_ep_values else None

    overall = _window_stats(client_rows, 0, args.duration_s)
    baseline = _window_stats(client_rows, 0, args.baseline_s)
    rollout_window = _window_stats(client_rows, args.baseline_s, args.duration_s)

    summary = {
        "profile": args.profile,
        "run_id": args.run_id,
        "rollout_trigger_s": args.baseline_s,
        "rollout_duration_s": args.rollout_elapsed_s,
        "ready_endpoints": {
            "min": min_ready_ep,
            "max": max_ready_ep,
        },
        "overall": overall,
        "baseline": baseline,
        "during_and_after_rollout": rollout_window,
        "failed_seconds": failed_seconds,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(f"=== Lab 04 Summary ({args.profile} / run {args.run_id}) ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
