from __future__ import annotations

import gc
import json
import os
import resource
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Histogram,
    generate_latest,
)
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily

LAB_PROFILE = os.getenv("LAB_PROFILE", "unknown")
CACHE_MODE = os.getenv("CACHE_MODE", "unbounded").lower()
PAYLOAD_KB = max(1, int(os.getenv("PAYLOAD_KB", "1024")))
CACHE_MAX_ENTRIES = max(1, int(os.getenv("CACHE_MAX_ENTRIES", "32")))
MEM_SOFT_LIMIT_PCT = float(os.getenv("MEM_SOFT_LIMIT_PCT", "75"))

REGISTRY = CollectorRegistry()

HTTP_REQUESTS_TOTAL = Counter(
    "http_requests_total",
    "Total HTTP requests handled by the Lab 02 service.",
    labelnames=("method", "path", "status", "profile"),
    registry=REGISTRY,
)

HTTP_REQUEST_DURATION_SECONDS = Histogram(
    "http_request_duration_seconds",
    "Wall-clock HTTP request duration in seconds.",
    labelnames=("method", "path", "profile"),
    buckets=(0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
    registry=REGISTRY,
)

CACHE_EVICTIONS_TOTAL = Counter(
    "app_cache_evictions_total",
    "Total entries evicted from the in-memory cache.",
    labelnames=("profile", "reason"),
    registry=REGISTRY,
)

LOAD_SHED_TOTAL = Counter(
    "app_load_shed_total",
    "Total ingest requests shed with HTTP 503 due to memory soft limit.",
    labelnames=("profile",),
    registry=REGISTRY,
)

CGROUP_V2_CURRENT = Path("/sys/fs/cgroup/memory.current")
CGROUP_V2_MAX = Path("/sys/fs/cgroup/memory.max")
CGROUP_V2_EVENTS = Path("/sys/fs/cgroup/memory.events")

CGROUP_V1_USAGE = Path("/sys/fs/cgroup/memory/memory.usage_in_bytes")
CGROUP_V1_LIMIT = Path("/sys/fs/cgroup/memory/memory.limit_in_bytes")
CGROUP_V1_OOM = Path("/sys/fs/cgroup/memory/memory.oom_control")

PROC_SELF_STATUS = Path("/proc/self/status")

_cache_lock = threading.Lock()
_cache: OrderedDict[int, bytearray] = OrderedDict()
_cache_bytes = 0
_next_id = 0


def _read_int_file(path: Path) -> int | None:
    try:
        raw = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if raw == "max":
        return 0
    return int(raw) if raw.isdigit() else None


def _parse_kv_file(path: Path) -> dict[str, int]:
    result: dict[str, int] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return result
    for line in lines:
        parts = line.split()
        if len(parts) == 2 and parts[1].isdigit():
            result[parts[0]] = int(parts[1])
    return result


def read_process_rss_bytes() -> int:
    """Read current resident set size (VmRSS) from /proc/self/status."""
    if PROC_SELF_STATUS.is_file():
        try:
            for line in PROC_SELF_STATUS.read_text(encoding="utf-8").splitlines():
                if line.startswith("VmRSS:"):
                    parts = line.split()
                    if len(parts) >= 2 and parts[1].isdigit():
                        return int(parts[1]) * 1024
        except OSError:
            pass
    # Non-Linux fallback (ru_maxrss is bytes on macOS, KiB on Linux).
    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def read_memory_stats() -> dict[str, Any]:
    """Read Linux cgroup memory telemetry plus process RSS and cache state."""
    cgroup_version = "unavailable"
    mem_current = 0
    mem_max = 0
    oom_events = 0
    oom_kills = 0

    if CGROUP_V2_CURRENT.is_file():
        cgroup_version = "v2"
        mem_current = _read_int_file(CGROUP_V2_CURRENT) or 0
        mem_max = _read_int_file(CGROUP_V2_MAX) or 0
        events = _parse_kv_file(CGROUP_V2_EVENTS)
        oom_events = events.get("oom", 0)
        oom_kills = events.get("oom_kill", 0)
    elif CGROUP_V1_USAGE.is_file():
        cgroup_version = "v1"
        mem_current = _read_int_file(CGROUP_V1_USAGE) or 0
        limit_val = _read_int_file(CGROUP_V1_LIMIT) or 0
        # Kernel reports huge sentinel (~2^63) when unlimited in cgroup v1.
        mem_max = 0 if limit_val >= (1 << 60) else limit_val
        oom_ctrl = _parse_kv_file(CGROUP_V1_OOM)
        oom_kills = oom_ctrl.get("oom_kill", 0)

    with _cache_lock:
        cache_entries = len(_cache)
        cache_bytes = _cache_bytes

    rss_bytes = read_process_rss_bytes()
    usage_pct = round((mem_current / mem_max) * 100.0, 2) if mem_max > 0 else 0.0

    return {
        "profile": LAB_PROFILE,
        "cache_mode": CACHE_MODE,
        "payload_kb": PAYLOAD_KB,
        "cache_max_entries": CACHE_MAX_ENTRIES if CACHE_MODE == "bounded_lru" else None,
        "mem_soft_limit_pct": (
            MEM_SOFT_LIMIT_PCT if CACHE_MODE == "bounded_lru" else None
        ),
        "cgroup_version": cgroup_version,
        "memory_current_bytes": mem_current,
        "memory_max_bytes": mem_max,
        "memory_usage_pct": usage_pct,
        "oom_events": oom_events,
        "oom_kills": oom_kills,
        "process_rss_bytes": rss_bytes,
        "cache_entries": cache_entries,
        "cache_bytes": cache_bytes,
    }


class MemoryStatsCollector:
    """Expose live cache, process RSS, and cgroup memory metrics to Prometheus."""

    def collect(self) -> Iterable[GaugeMetricFamily | CounterMetricFamily]:
        stats = read_memory_stats()

        entries = GaugeMetricFamily(
            "app_cache_entries",
            "Current number of payload entries retained in the in-memory cache.",
            labels=["profile"],
        )
        entries.add_metric([LAB_PROFILE], float(stats["cache_entries"]))
        yield entries

        cache_bytes = GaugeMetricFamily(
            "app_cache_bytes",
            "Current total bytes retained in the in-memory cache.",
            labels=["profile"],
        )
        cache_bytes.add_metric([LAB_PROFILE], float(stats["cache_bytes"]))
        yield cache_bytes

        rss = GaugeMetricFamily(
            "app_process_rss_bytes",
            "Current worker process resident set size (VmRSS) in bytes.",
            labels=["profile"],
        )
        rss.add_metric([LAB_PROFILE], float(stats["process_rss_bytes"]))
        yield rss

        mem_current = GaugeMetricFamily(
            "cgroup_memory_current_bytes",
            "Current container cgroup memory usage in bytes.",
            labels=["profile"],
        )
        mem_current.add_metric([LAB_PROFILE], float(stats["memory_current_bytes"]))
        yield mem_current

        mem_max = GaugeMetricFamily(
            "cgroup_memory_max_bytes",
            "Configured container cgroup memory limit in bytes (0 if unlimited).",
            labels=["profile"],
        )
        mem_max.add_metric([LAB_PROFILE], float(stats["memory_max_bytes"]))
        yield mem_max

        oom_kills = CounterMetricFamily(
            "cgroup_memory_oom_kills_total",
            "Total kernel OOM kills recorded in cgroup memory.events.",
            labels=["profile"],
        )
        oom_kills.add_metric([LAB_PROFILE], float(stats["oom_kills"]))
        yield oom_kills


REGISTRY.register(MemoryStatsCollector())


def _allocate_resident_payload(size_bytes: int) -> bytearray:
    """Allocate non-zero bytes so Linux faults physical pages into RSS immediately."""
    return bytearray(b"\x5a") * size_bytes


def _handle_ingest() -> tuple[str, dict[str, Any], list[tuple[str, str]]]:
    global _cache_bytes, _next_id

    payload_bytes = PAYLOAD_KB * 1024
    extra_headers: list[tuple[str, str]] = []

    if CACHE_MODE == "bounded_lru":
        with _cache_lock:
            stats = read_memory_stats_unlocked()
            mem_current = stats["memory_current_bytes"]
            mem_max = stats["memory_max_bytes"]

            if mem_max > 0:
                projected_pct = ((mem_current + payload_bytes) / mem_max) * 100.0
                if projected_pct >= MEM_SOFT_LIMIT_PCT:
                    evicted_for_pressure = 0
                    while _cache and evicted_for_pressure < max(1, len(_cache) // 4):
                        _, evicted = _cache.popitem(last=False)
                        _cache_bytes -= len(evicted)
                        evicted_for_pressure += 1
                    if evicted_for_pressure > 0:
                        CACHE_EVICTIONS_TOTAL.labels(
                            profile=LAB_PROFILE,
                            reason="memory_pressure",
                        ).inc(evicted_for_pressure)
                        gc.collect()

                    refreshed = read_memory_stats_unlocked()
                    refreshed_pct = (
                        (refreshed["memory_current_bytes"] + payload_bytes)
                        / mem_max
                    ) * 100.0
                    if refreshed_pct >= MEM_SOFT_LIMIT_PCT:
                        LOAD_SHED_TOTAL.labels(profile=LAB_PROFILE).inc()
                        extra_headers.append(("Retry-After", "1"))
                        return (
                            "503 Service Unavailable",
                            {
                                "error": "memory_soft_limit_exceeded",
                                "profile": LAB_PROFILE,
                                "memory_usage_pct": round(refreshed_pct, 2),
                                "soft_limit_pct": MEM_SOFT_LIMIT_PCT,
                            },
                            extra_headers,
                        )

            while len(_cache) >= CACHE_MAX_ENTRIES:
                _, evicted = _cache.popitem(last=False)
                _cache_bytes -= len(evicted)
                CACHE_EVICTIONS_TOTAL.labels(
                    profile=LAB_PROFILE,
                    reason="lru_capacity",
                ).inc()

            payload = _allocate_resident_payload(payload_bytes)
            _next_id += 1
            entry_id = _next_id
            _cache[entry_id] = payload
            _cache_bytes += len(payload)
            entries = len(_cache)
            total_bytes = _cache_bytes

        return (
            "200 OK",
            {
                "status": "stored",
                "profile": LAB_PROFILE,
                "cache_mode": CACHE_MODE,
                "entry_id": entry_id,
                "payload_bytes": payload_bytes,
                "cache_entries": entries,
                "cache_bytes": total_bytes,
            },
            extra_headers,
        )

    # Unbounded mode: retain every allocation without eviction -> triggers OOM kill.
    payload = _allocate_resident_payload(payload_bytes)
    with _cache_lock:
        _next_id += 1
        entry_id = _next_id
        _cache[entry_id] = payload
        _cache_bytes += len(payload)
        entries = len(_cache)
        total_bytes = _cache_bytes

    return (
        "200 OK",
        {
            "status": "stored",
            "profile": LAB_PROFILE,
            "cache_mode": CACHE_MODE,
            "entry_id": entry_id,
            "payload_bytes": payload_bytes,
            "cache_entries": entries,
            "cache_bytes": total_bytes,
        },
        extra_headers,
    )


def read_memory_stats_unlocked() -> dict[str, int]:
    """Fast cgroup memory read used under _cache_lock during admission control."""
    if CGROUP_V2_CURRENT.is_file():
        return {
            "memory_current_bytes": _read_int_file(CGROUP_V2_CURRENT) or 0,
            "memory_max_bytes": _read_int_file(CGROUP_V2_MAX) or 0,
        }
    if CGROUP_V1_USAGE.is_file():
        limit_val = _read_int_file(CGROUP_V1_LIMIT) or 0
        return {
            "memory_current_bytes": _read_int_file(CGROUP_V1_USAGE) or 0,
            "memory_max_bytes": 0 if limit_val >= (1 << 60) else limit_val,
        }
    return {"memory_current_bytes": 0, "memory_max_bytes": 0}


def _json_response(
    start_response: Callable[..., Any],
    status: str,
    payload: dict[str, Any],
    extra_headers: list[tuple[str, str]] | None = None,
) -> list[bytes]:
    body = (json.dumps(payload) + "\n").encode("utf-8")
    headers = [
        ("Content-Type", "application/json"),
        ("Content-Length", str(len(body))),
    ]
    if extra_headers:
        headers.extend(extra_headers)
    start_response(status, headers)
    return [body]


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
            {"status": "ok", "profile": LAB_PROFILE, "cache_mode": CACHE_MODE},
        )

    if path == "/memory-stats":
        return _json_response(start_response, "200 OK", read_memory_stats())

    if path == "/metrics":
        payload = generate_latest(REGISTRY)
        start_response(
            "200 OK",
            [
                ("Content-Type", CONTENT_TYPE_LATEST),
                ("Content-Length", str(len(payload))),
            ],
        )
        return [payload]

    if path == "/ingest":
        t0 = time.perf_counter()
        status, payload, extra_headers = _handle_ingest()
        elapsed_s = time.perf_counter() - t0
        status_code = status.split()[0]

        HTTP_REQUESTS_TOTAL.labels(
            method=method,
            path="/ingest",
            status=status_code,
            profile=LAB_PROFILE,
        ).inc()
        HTTP_REQUEST_DURATION_SECONDS.labels(
            method=method,
            path="/ingest",
            profile=LAB_PROFILE,
        ).observe(elapsed_s)

        return _json_response(start_response, status, payload, extra_headers)

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
