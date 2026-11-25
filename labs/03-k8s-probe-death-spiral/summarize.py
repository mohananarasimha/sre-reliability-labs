#!/usr/bin/env python3
"""Summarize Lab 03 Kubernetes probe death-spiral experiment artifacts."""

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


def _phase_stats(
    rows: list[dict[str, Any]],
    start_s: int,
    end_s: int,
) -> dict[str, Any]:
    sliced = [r for r in rows if start_s <= int(r["sec"]) < end_s]
    total = sum(int(r["total"]) for r in sliced)
    ok = sum(int(r["ok"]) for r in sliced)
    http_5xx = sum(int(r["http_5xx"]) for r in sliced)
    http_other = sum(int(r["http_other"]) for r in sliced)
    conn_err = sum(int(r["conn_err"]) for r in sliced)
    timeout = sum(int(r["timeout"]) for r in sliced)
    success_rate = round((ok / total) * 100.0, 2) if total > 0 else 0.0
    return {
        "window_s": f"{start_s}-{end_s}",
        "total": total,
        "ok": ok,
        "http_5xx": http_5xx,
        "http_other": http_other,
        "conn_err": conn_err,
        "timeout": timeout,
        "success_rate_pct": success_rate,
    }


def _client_recovery_seconds(
    rows: list[dict[str, Any]],
    spike_end_s: int,
    consecutive_ok_s: int = 3,
) -> int | None:
    """Seconds after spike_end_s until client sees N consecutive 100% ok seconds."""
    recovery_rows = sorted(
        (r for r in rows if int(r["sec"]) >= spike_end_s),
        key=lambda r: int(r["sec"]),
    )
    streak = 0
    streak_start_sec: int | None = None
    for row in recovery_rows:
        sec = int(row["sec"])
        total = int(row["total"])
        ok = int(row["ok"])
        if total > 0 and ok == total:
            if streak == 0:
                streak_start_sec = sec
            streak += 1
            if streak >= consecutive_ok_s and streak_start_sec is not None:
                return streak_start_sec - spike_end_s
        else:
            streak = 0
            streak_start_sec = None
    return None


def _endpoints_recovery_seconds(
    timeline: list[dict[str, Any]],
    spike_end_s: int,
    expected_endpoints: int = 3,
) -> int | None:
    """Seconds after spike_end_s until ready endpoints stay at expected_endpoints."""
    after = sorted(
        (t for t in timeline if int(t["elapsed_s"]) >= spike_end_s),
        key=lambda t: int(t["elapsed_s"]),
    )
    if not after:
        return None
    # Find the earliest timestamp after spike_end_s from which ready_endpoints
    # remains == expected_endpoints for the remainder of the observation window.
    candidate: int | None = None
    for sample in after:
        if int(sample["ready_endpoints"]) >= expected_endpoints:
            if candidate is None:
                candidate = int(sample["elapsed_s"])
        else:
            candidate = None
    return (candidate - spike_end_s) if candidate is not None else None


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize Lab 03 experiment run.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--baseline-s", type=int, default=20)
    parser.add_argument("--spike-s", type=int, default=60)
    parser.add_argument("--duration-s", type=int, default=150)
    parser.add_argument("--client-jsonl", required=True)
    parser.add_argument("--timeline-jsonl", required=True)
    parser.add_argument("--events-json", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    spike_start_s = args.baseline_s
    spike_end_s = args.baseline_s + args.spike_s

    client_rows = _load_jsonl(Path(args.client_jsonl))
    timeline = _load_jsonl(Path(args.timeline_jsonl))
    events_doc = json.loads(Path(args.events_json).read_text(encoding="utf-8"))

    per_pod_restarts: dict[str, int] = {}
    pod_names_seen: set[str] = set()
    crashloop_observed = False

    for sample in timeline:
        for pod in sample.get("pods", []):
            name = str(pod["name"])
            pod_names_seen.add(name)
            restarts = int(pod.get("restarts", 0))
            per_pod_restarts.setdefault(name, 0)
            if restarts > per_pod_restarts[name]:
                per_pod_restarts[name] = restarts
            if pod.get("waiting_reason") == "CrashLoopBackOff":
                crashloop_observed = True

    def _min_ep(samples: list[dict[str, Any]]) -> int | None:
        if not samples:
            return None
        return min(int(s["ready_endpoints"]) for s in samples)

    ep_baseline = _min_ep(
        [s for s in timeline if int(s["elapsed_s"]) < spike_start_s]
    )
    ep_spike = _min_ep(
        [s for s in timeline if spike_start_s <= int(s["elapsed_s"]) < spike_end_s]
    )
    ep_recovery = _min_ep(
        [s for s in timeline if int(s["elapsed_s"]) >= spike_end_s]
    )
    ep_overall = _min_ep(timeline)

    event_counts = {"Unhealthy": 0, "Killing": 0, "BackOff": 0}
    for item in events_doc.get("items", []):
        obj_name = item.get("involvedObject", {}).get("name", "")
        if pod_names_seen and obj_name not in pod_names_seen:
            continue
        reason = item.get("reason", "")
        if reason in event_counts:
            count = item.get("count") or 1
            event_counts[reason] += int(count)

    client_rec_s = _client_recovery_seconds(client_rows, spike_end_s)
    ep_rec_s = _endpoints_recovery_seconds(timeline, spike_end_s)
    if client_rec_s is not None and ep_rec_s is not None:
        full_rec_s: int | None = max(client_rec_s, ep_rec_s)
    else:
        full_rec_s = None

    summary = {
        "profile": args.profile,
        "run_id": args.run_id,
        "phases_s": {
            "baseline": [0, spike_start_s],
            "spike": [spike_start_s, spike_end_s],
            "recovery": [spike_end_s, args.duration_s],
        },
        "total_restarts": sum(per_pod_restarts.values()),
        "per_pod_restarts": dict(sorted(per_pod_restarts.items())),
        "crashloop_observed": crashloop_observed,
        "ready_endpoints_min": {
            "overall": ep_overall,
            "baseline": ep_baseline,
            "spike": ep_spike,
            "recovery": ep_recovery,
        },
        "events": event_counts,
        "client_by_phase": {
            "baseline": _phase_stats(client_rows, 0, spike_start_s),
            "spike": _phase_stats(client_rows, spike_start_s, spike_end_s),
            "recovery": _phase_stats(client_rows, spike_end_s, args.duration_s),
        },
        "recovery_after_spike_end_s": {
            "client_100pct_ok_s": client_rec_s,
            "endpoints_3_of_3_s": ep_rec_s,
            "full_recovery_s": full_rec_s,
        },
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print(f"=== Lab 03 Summary ({args.profile} / run {args.run_id}) ===")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
