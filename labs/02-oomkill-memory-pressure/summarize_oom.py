#!/usr/bin/env python3
"""Summarize Docker OOM/die events, RestartCount, and memory.events oom_kill delta."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


def parse_docker_events(events_path: Path) -> tuple[int, int, list[dict[str, Any]]]:
    oom_count = 0
    die_137_count = 0
    parsed: list[dict[str, Any]] = []
    if not events_path.is_file():
        return 0, 0, parsed

    for raw_line in events_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        event = json.loads(line)
        action = str(event.get("Action") or event.get("status") or "")
        attrs = event.get("Actor", {}).get("Attributes", {}) or {}
        exit_code = attrs.get("exitCode")
        if action == "oom":
            oom_count += 1
        elif action == "die" and str(exit_code) == "137":
            die_137_count += 1
        parsed.append(
            {
                "time": event.get("time"),
                "action": action,
                "exit_code": exit_code,
            }
        )
    return oom_count, die_137_count, parsed


def main() -> None:
    parser = argparse.ArgumentParser(description="Summarize Lab 02 OOM evidence.")
    parser.add_argument("--profile", required=True)
    parser.add_argument("--container", required=True)
    parser.add_argument("--events-file", required=True)
    parser.add_argument("--inspect-file", required=True)
    parser.add_argument("--oom-before", type=int, default=0)
    parser.add_argument("--oom-after", type=int, default=-1)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    oom_events, die_137_events, events_list = parse_docker_events(
        Path(args.events_file)
    )

    inspect_raw = json.loads(Path(args.inspect_file).read_text(encoding="utf-8"))
    info = inspect_raw[0] if isinstance(inspect_raw, list) and inspect_raw else {}
    state = info.get("State", {})

    oom_after = args.oom_after if args.oom_after >= 0 else None
    oom_delta = (oom_after - args.oom_before) if oom_after is not None else None

    summary = {
        "profile": args.profile,
        "container": args.container,
        "docker_events_oom_count": oom_events,
        "docker_events_die_exit_137_count": die_137_events,
        "docker_events": events_list,
        "restart_count": info.get("RestartCount"),
        "state_status": state.get("Status"),
        "state_oom_killed": state.get("OOMKilled"),
        "state_exit_code": state.get("ExitCode"),
        "memory_events_oom_kill_before": args.oom_before,
        "memory_events_oom_kill_after": oom_after,
        "memory_events_oom_kill_delta": oom_delta,
    }

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    print("--- OOM / Docker event summary ---")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
