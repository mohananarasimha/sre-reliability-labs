"""Summarize one Lab 05 run: outcomes by phase, recovery time, retry amplification."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

RECOVERY_SUCCESS = 0.99
RECOVERY_WINDOW_S = 5


def _load_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _success(rows: list[dict]) -> tuple[int, int]:
    return sum(r["ok"] for r in rows), sum(r["total"] for r in rows)


def _pct(ok: int, total: int) -> float:
    return round(100.0 * ok / total, 2) if total else 0.0


def _stat_at(stats: list[dict], start: float, t: float) -> dict | None:
    """Last stats sample taken at or before t seconds into the run."""
    best = None
    for s in stats:
        if s["epoch"] - start <= t and s["backend"] and s["frontend"]:
            best = s
    return best


def _delta(a: dict | None, b: dict | None, side: str, key: str) -> int | None:
    if not a or not b:
        return None
    return b[side][key] - a[side][key]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--meta", type=Path, required=True)
    ap.add_argument("--client", type=Path, required=True)
    ap.add_argument("--stats", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()

    meta = json.loads(args.meta.read_text())
    client = [r for r in _load_jsonl(args.client) if "sec" in r]
    stats = _load_jsonl(args.stats)
    start = meta["start_epoch"]
    trig = meta["trigger_at_s"]
    trig_end = trig + meta["trigger_seconds"]
    duration = meta["duration_s"]

    phases = {
        "baseline": [r for r in client if r["sec"] < trig],
        "trigger": [r for r in client if trig <= r["sec"] < trig_end],
        "after_trigger": [r for r in client if r["sec"] >= trig_end],
        "last_30s": [r for r in client if r["sec"] >= duration - 30],
    }
    phase_summary = {}
    for name, rows in phases.items():
        ok, total = _success(rows)
        phase_summary[name] = {"ok": ok, "total": total, "success_pct": _pct(ok, total)}

    recovered_at = None
    after = sorted(phases["after_trigger"], key=lambda r: r["sec"])
    for i in range(len(after) - RECOVERY_WINDOW_S + 1):
        window = after[i : i + RECOVERY_WINDOW_S]
        if all(r["total"] and r["ok"] / r["total"] >= RECOVERY_SUCCESS for r in window):
            recovered_at = window[0]["sec"]
            break

    s_end_trig = _stat_at(stats, start, trig_end)
    s_last30 = _stat_at(stats, start, duration - 30)
    s_end = _stat_at(stats, start, duration)
    amp = {}
    windows = (("after_trigger", s_end_trig, s_end), ("last_30s", s_last30, s_end))
    for label, a, b in windows:
        reqs = _delta(a, b, "frontend", "requests_total")
        attempts = _delta(a, b, "frontend", "attempts_total")
        amp[label] = {
            "frontend_requests": reqs,
            "backend_attempts": attempts,
            "attempts_per_request": round(attempts / reqs, 2) if reqs else None,
            "backend_rejected_503": _delta(a, b, "backend", "rejected_total"),
            "budget_denied": _delta(a, b, "frontend", "budget_denied_total"),
        }

    waiting = [
        (round(s["epoch"] - start), s["backend"]["waiting"])
        for s in stats
        if s["backend"]
    ]
    summary = {
        "policy": meta["policy"],
        "run_id": meta["run_id"],
        "trigger": (
            f"+{meta['trigger_extra_ms']}ms for {meta['trigger_seconds']}s at t={trig}s"
        ),
        "trigger_fired_at_s": round(meta["trigger_epoch"] - start, 2),
        "phases": phase_summary,
        "recovered_at_s": recovered_at,
        "recovery_after_trigger_end_s": (
            None if recovered_at is None else recovered_at - trig_end
        ),
        "amplification": amp,
        "backend_waiting_max": max((w for _, w in waiting), default=None),
        "backend_waiting_at_end": waiting[-1][1] if waiting else None,
        "late_sends_total": sum(r["late"] for r in client),
        "timeline_5s": [
            {
                "t": f"{t0}-{t0 + 4}",
                "success_pct": _pct(
                    *_success([r for r in client if t0 <= r["sec"] < t0 + 5])
                ),
                "backend_waiting": next((w for t, w in waiting if t >= t0 + 4), None),
            }
            for t0 in range(0, duration, 5)
        ],
    }
    args.out.write_text(json.dumps(summary, indent=2) + "\n")
    headline = {k: v for k, v in summary.items() if k != "timeline_5s"}
    print(json.dumps(headline, indent=2))
    print("t(s)      success%  backend_waiting")
    for row in summary["timeline_5s"]:
        print(f"{row['t']:<9} {row['success_pct']:>8}  {row['backend_waiting']}")


if __name__ == "__main__":
    main()
