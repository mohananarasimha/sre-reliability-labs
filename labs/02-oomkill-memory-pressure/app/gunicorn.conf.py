from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

CGROUP_V2_EVENTS = Path("/sys/fs/cgroup/memory.events")
CGROUP_V1_OOM = Path("/sys/fs/cgroup/memory/memory.oom_control")

_baseline_oom_kills = 0


def _read_oom_kills() -> int:
    for path in (CGROUP_V2_EVENTS, CGROUP_V1_OOM):
        if path.is_file():
            try:
                for line in path.read_text(encoding="utf-8").splitlines():
                    parts = line.split()
                    if (
                        len(parts) == 2
                        and parts[0] == "oom_kill"
                        and parts[1].isdigit()
                    ):
                        return int(parts[1])
            except OSError:
                return 0
    return 0


def when_ready(server: Any) -> None:
    """Record baseline cgroup oom_kill counter at master startup."""
    global _baseline_oom_kills
    _baseline_oom_kills = _read_oom_kills()


def child_exit(server: Any, worker: Any) -> None:
    """Propagate worker OOM kills to container exit 137 instead of masking them."""
    current_oom_kills = _read_oom_kills()
    if current_oom_kills > _baseline_oom_kills:
        sys.stderr.write(
            f"[lab02] Worker pid={worker.pid} terminated by kernel OOM killer "
            f"(oom_kill={current_oom_kills}); exiting master with code 137.\n"
        )
        sys.stderr.flush()
        os._exit(137)
