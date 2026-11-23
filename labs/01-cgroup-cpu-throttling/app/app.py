from __future__ import annotations

import hashlib
import json
import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
    multiprocess,
)
from prometheus_client.core import CounterMetricFamily

LAB_PROFILE = os.getenv("LAB_PROFILE", "unknown")
DEFAULT_WORK_MS = float(os.getenv("WORK_MS", "15"))
WORKERS = int(os.getenv("WORKERS", "1"))

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the Lab 01 service.",
    labelnames=("method", "path", "status", "profile"),
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "Wall-clock HTTP request duration in seconds.",
    labelnames=("method", "path", "profile"),
    buckets=(
        0.005,
        0.01,
        0.02,
        0.03,
        0.05,
        0.075,
        0.1,
        0.15,
        0.25,
        0.5,
        1.0,
        2.0,
        5.0,
    ),
)

CGROUP_V2_STAT = Path("/sys/fs/cgroup/cpu.stat")
CGROUP_V2_MAX = Path("/sys/fs/cgroup/cpu.max")
CGROUP_V1_STAT = Path("/sys/fs/cgroup/cpu/cpu.stat")
CGROUP_V1_QUOTA = Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us")
CGROUP_V1_PERIOD = Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us")


def read_cgroup_cpu_stats() -> dict[str, Any]:
    """Read Linux CFS throttling counters from cgroup v2 (with cgroup v1 fallback)."""
    stats: dict[str, Any] = {
        "profile": LAB_PROFILE,
        "workers": WORKERS,
        "work_ms": DEFAULT_WORK_MS,
        "cgroup_version": "unavailable",
        "cpu_stat_path": None,
        "cpu_max": "unknown",
        "nr_periods": 0,
        "nr_throttled": 0,
        "throttled_usec": 0,
        "throttled_seconds": 0.0,
        "throttled_ratio": 0.0,
    }

    if CGROUP_V2_STAT.is_file():
        stats["cgroup_version"] = "v2"
        stats["cpu_stat_path"] = str(CGROUP_V2_STAT)
        parsed = _parse_kv_file(CGROUP_V2_STAT)
        stats["nr_periods"] = parsed.get("nr_periods", 0)
        stats["nr_throttled"] = parsed.get("nr_throttled", 0)
        stats["throttled_usec"] = parsed.get("throttled_usec", 0)
        if CGROUP_V2_MAX.is_file():
            stats["cpu_max"] = CGROUP_V2_MAX.read_text(encoding="utf-8").strip()
    elif CGROUP_V1_STAT.is_file():
        stats["cgroup_version"] = "v1"
        stats["cpu_stat_path"] = str(CGROUP_V1_STAT)
        parsed = _parse_kv_file(CGROUP_V1_STAT)
        stats["nr_periods"] = parsed.get("nr_periods", 0)
        stats["nr_throttled"] = parsed.get("nr_throttled", 0)
        # cgroup v1 exports throttled_time in nanoseconds.
        stats["throttled_usec"] = parsed.get("throttled_time", 0) // 1000
        if CGROUP_V1_QUOTA.is_file() and CGROUP_V1_PERIOD.is_file():
            quota = CGROUP_V1_QUOTA.read_text(encoding="utf-8").strip()
            period = CGROUP_V1_PERIOD.read_text(encoding="utf-8").strip()
            stats["cpu_max"] = f"{quota} {period}"

    stats["throttled_seconds"] = round(stats["throttled_usec"] / 1_000_000.0, 6)
    if stats["nr_periods"] > 0:
        stats["throttled_ratio"] = round(stats["nr_throttled"] / stats["nr_periods"], 6)
    return stats


def _parse_kv_file(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            result[parts[0]] = int(parts[1])
    return result


class CgroupCpuCollector:
    """Expose live container-wide cgroup CFS throttling counters to Prometheus."""

    def collect(self) -> Iterable[CounterMetricFamily]:
        data = read_cgroup_cpu_stats()

        nr_periods = CounterMetricFamily(
            "cgroup_cpu_nr_periods_total",
            "Total CFS enforcement intervals elapsed for this container cgroup.",
            labels=["profile"],
        )
        nr_periods.add_metric([LAB_PROFILE], float(data["nr_periods"]))
        yield nr_periods

        nr_throttled = CounterMetricFamily(
            "cgroup_cpu_nr_throttled_total",
            "Number of CFS periods in which this container cgroup was throttled.",
            labels=["profile"],
        )
        nr_throttled.add_metric([LAB_PROFILE], float(data["nr_throttled"]))
        yield nr_throttled

        throttled_seconds = CounterMetricFamily(
            "cgroup_cpu_throttled_seconds_total",
            "Total seconds tasks in this container cgroup were CFS throttled.",
            labels=["profile"],
        )
        throttled_seconds.add_metric([LAB_PROFILE], float(data["throttled_seconds"]))
        yield throttled_seconds


def burn_cpu_ms(target_ms: float) -> float:
    """Burn thread CPU time so CFS throttling extends wall-clock latency."""
    target_s = target_ms / 1000.0
    cpu_start = time.thread_time()
    digest = b"sre-reliability-labs-cfs-work"
    while (time.thread_time() - cpu_start) < target_s:
        for _ in range(200):
            digest = hashlib.sha256(digest).digest()
    return (time.thread_time() - cpu_start) * 1000.0


def _json_response(
    start_response: Callable[..., Any],
    status: str,
    payload: dict[str, Any],
) -> list[bytes]:
    body = (json.dumps(payload) + "\n").encode("utf-8")
    start_response(
        status,
        [
            ("Content-Type", "application/json"),
            ("Content-Length", str(len(body))),
        ],
    )
    return [body]


def _parse_work_ms(query_string: str) -> float:
    params = parse_qs(query_string)
    raw = params.get("work_ms", [None])[0]
    if raw is None:
        return DEFAULT_WORK_MS
    try:
        parsed = float(raw)
    except ValueError:
        return DEFAULT_WORK_MS
    return max(1.0, min(parsed, 200.0))


def application(
    environ: dict[str, Any],
    start_response: Callable[..., Any],
) -> list[bytes]:
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/")

    if path == "/healthz":
        return _json_response(
            start_response,
            "200 OK",
            {"status": "ok", "profile": LAB_PROFILE},
        )

    if path == "/cgroup-stats":
        return _json_response(start_response, "200 OK", read_cgroup_cpu_stats())

    if path == "/metrics":
        registry = CollectorRegistry()
        if os.getenv("PROMETHEUS_MULTIPROC_DIR"):
            multiprocess.MultiProcessCollector(registry)
        registry.register(CgroupCpuCollector())
        payload = generate_latest(registry)
        start_response(
            "200 OK",
            [
                ("Content-Type", CONTENT_TYPE_LATEST),
                ("Content-Length", str(len(payload))),
            ],
        )
        return [payload]

    if path == "/work":
        work_ms = _parse_work_ms(environ.get("QUERY_STRING", ""))
        wall_start = time.perf_counter()
        cpu_ms = burn_cpu_ms(work_ms)
        wall_s = time.perf_counter() - wall_start

        HTTP_REQUESTS_TOTAL.labels(
            method=method,
            path="/work",
            status="200",
            profile=LAB_PROFILE,
        ).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(
            method=method,
            path="/work",
            profile=LAB_PROFILE,
        ).observe(wall_s)

        return _json_response(
            start_response,
            "200 OK",
            {
                "profile": LAB_PROFILE,
                "pid": os.getpid(),
                "work_ms_target": round(work_ms, 2),
                "cpu_ms": round(cpu_ms, 2),
                "wall_ms": round(wall_s * 1000.0, 2),
            },
        )

    HTTP_REQUESTS_TOTAL.labels(
        method=method,
        path=path,
        status="404",
        profile=LAB_PROFILE,
    ).inc()
    return _json_response(
        start_response,
        "404 Not Found",
        {"error": "not_found", "path": path},
    )
