from __future__ import annotations

import hashlib
import json
import os
import threading
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
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

LAB_PROFILE = os.getenv("LAB_PROFILE", "unknown")
DEFAULT_WORK_MS = float(os.getenv("WORK_MS", "20"))
DEFAULT_PARALLELISM = max(1, int(os.getenv("PARALLELISM", "4")))
WORKERS = int(os.getenv("WORKERS", "1"))

# Preallocated 1 MiB buffer hashed via OpenSSL/hashlib so the GIL is released
# (>2048 B threshold in CPython) and worker threads burn CPU truly in parallel.
_HASH_BUFFER = b"\x5a" * (1024 * 1024)

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
CGROUP_V1_USAGE = Path("/sys/fs/cgroup/cpuacct/cpuacct.usage")


def _parse_kv_file(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            result[parts[0]] = int(parts[1])
    return result


def _parse_cpu_max(raw: str) -> tuple[int | None, int | None]:
    parts = raw.split()
    if len(parts) != 2:
        return None, None
    quota = int(parts[0]) if parts[0].isdigit() else None
    period = int(parts[1]) if parts[1].isdigit() else None
    return quota, period


def read_cgroup_cpu_stats() -> dict[str, Any]:
    """Read Linux CFS counters and usage_usec from cgroup v2 (with v1 fallback)."""
    stats: dict[str, Any] = {
        "profile": LAB_PROFILE,
        "workers": WORKERS,
        "parallelism": DEFAULT_PARALLELISM,
        "work_ms": DEFAULT_WORK_MS,
        "cgroup_version": "unavailable",
        "cpu_stat_path": None,
        "cpu_max": "unknown",
        "cpu_quota_usec": None,
        "cpu_period_usec": None,
        "usage_usec": 0,
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
        stats["usage_usec"] = parsed.get("usage_usec", 0)
        stats["nr_periods"] = parsed.get("nr_periods", 0)
        stats["nr_throttled"] = parsed.get("nr_throttled", 0)
        stats["throttled_usec"] = parsed.get("throttled_usec", 0)
        if CGROUP_V2_MAX.is_file():
            cpu_max = CGROUP_V2_MAX.read_text(encoding="utf-8").strip()
            stats["cpu_max"] = cpu_max
            quota, period = _parse_cpu_max(cpu_max)
            stats["cpu_quota_usec"] = quota
            stats["cpu_period_usec"] = period
    elif CGROUP_V1_STAT.is_file():
        stats["cgroup_version"] = "v1"
        stats["cpu_stat_path"] = str(CGROUP_V1_STAT)
        parsed = _parse_kv_file(CGROUP_V1_STAT)
        stats["nr_periods"] = parsed.get("nr_periods", 0)
        stats["nr_throttled"] = parsed.get("nr_throttled", 0)
        # cgroup v1 exports throttled_time and cpuacct.usage in nanoseconds.
        stats["throttled_usec"] = parsed.get("throttled_time", 0) // 1000
        if CGROUP_V1_USAGE.is_file():
            raw_usage = CGROUP_V1_USAGE.read_text(encoding="utf-8").strip()
            if raw_usage.isdigit():
                stats["usage_usec"] = int(raw_usage) // 1000
        if CGROUP_V1_QUOTA.is_file() and CGROUP_V1_PERIOD.is_file():
            quota_raw = CGROUP_V1_QUOTA.read_text(encoding="utf-8").strip()
            period_raw = CGROUP_V1_PERIOD.read_text(encoding="utf-8").strip()
            stats["cpu_max"] = f"{quota_raw} {period_raw}"
            stats["cpu_quota_usec"] = (
                int(quota_raw) if quota_raw.lstrip("-").isdigit() else None
            )
            stats["cpu_period_usec"] = (
                int(period_raw) if period_raw.isdigit() else None
            )

    stats["throttled_seconds"] = round(stats["throttled_usec"] / 1_000_000.0, 6)
    if stats["nr_periods"] > 0:
        stats["throttled_ratio"] = round(stats["nr_throttled"] / stats["nr_periods"], 6)
    return stats


class CgroupCpuCollector:
    """Expose live container-wide cgroup CFS counters and quota to Prometheus."""

    def collect(self) -> Iterable[CounterMetricFamily | GaugeMetricFamily]:
        data = read_cgroup_cpu_stats()

        usage_usec = CounterMetricFamily(
            "cgroup_cpu_usage_usec_total",
            "Cumulative CPU usage in microseconds reported by cgroup cpu.stat.",
            labels=["profile"],
        )
        usage_usec.add_metric([LAB_PROFILE], float(data["usage_usec"]))
        yield usage_usec

        usage_seconds = CounterMetricFamily(
            "cgroup_cpu_usage_seconds_total",
            "Cumulative CPU usage in seconds reported by cgroup cpu.stat.",
            labels=["profile"],
        )
        usage_seconds.add_metric([LAB_PROFILE], float(data["usage_usec"]) / 1_000_000.0)
        yield usage_seconds

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

        throttled_usec = CounterMetricFamily(
            "cgroup_cpu_throttled_usec_total",
            "Total microseconds tasks in this container cgroup were CFS throttled.",
            labels=["profile"],
        )
        throttled_usec.add_metric([LAB_PROFILE], float(data["throttled_usec"]))
        yield throttled_usec

        throttled_seconds = CounterMetricFamily(
            "cgroup_cpu_throttled_seconds_total",
            "Total seconds tasks in this container cgroup were CFS throttled.",
            labels=["profile"],
        )
        throttled_seconds.add_metric([LAB_PROFILE], float(data["throttled_seconds"]))
        yield throttled_seconds

        quota_usec = GaugeMetricFamily(
            "cgroup_cpu_quota_usec",
            "Configured cgroup CFS CPU quota per period in microseconds (0 if max).",
            labels=["profile"],
        )
        quota_usec.add_metric([LAB_PROFILE], float(data["cpu_quota_usec"] or 0))
        yield quota_usec

        period_usec = GaugeMetricFamily(
            "cgroup_cpu_period_usec",
            "Configured cgroup CFS CPU period in microseconds.",
            labels=["profile"],
        )
        period_usec.add_metric([LAB_PROFILE], float(data["cpu_period_usec"] or 0))
        yield period_usec


def burn_parallel_cpu_ms(target_ms: float, parallelism: int) -> list[float]:
    """Spawn `parallelism` threads that simultaneously burn `target_ms` CPU each."""
    target_s = target_ms / 1000.0
    barrier = threading.Barrier(parallelism)
    thread_cpu_ms: list[float] = [0.0] * parallelism

    def _worker(idx: int) -> None:
        barrier.wait()
        cpu_start = time.thread_time()
        while (time.thread_time() - cpu_start) < target_s:
            hashlib.sha256(_HASH_BUFFER).digest()
        thread_cpu_ms[idx] = (time.thread_time() - cpu_start) * 1000.0

    threads = [
        threading.Thread(target=_worker, args=(idx,), daemon=True)
        for idx in range(parallelism)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    return thread_cpu_ms


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
            {
                "status": "ok",
                "profile": LAB_PROFILE,
                "workers": WORKERS,
                "parallelism": DEFAULT_PARALLELISM,
                "work_ms": DEFAULT_WORK_MS,
            },
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
        per_thread_cpu_ms = burn_parallel_cpu_ms(work_ms, DEFAULT_PARALLELISM)
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
                "parallelism": DEFAULT_PARALLELISM,
                "work_ms_target_per_thread": round(work_ms, 2),
                "total_cpu_ms": round(sum(per_thread_cpu_ms), 2),
                "per_thread_cpu_ms": [round(val, 2) for val in per_thread_cpu_ms],
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
