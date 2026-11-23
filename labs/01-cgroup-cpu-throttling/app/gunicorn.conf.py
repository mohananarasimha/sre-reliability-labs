from __future__ import annotations

from typing import Any

from prometheus_client import multiprocess


def child_exit(server: Any, worker: Any) -> None:
    """Clean up dead worker metric files in PROMETHEUS_MULTIPROC_DIR."""
    multiprocess.mark_process_dead(worker.pid)
