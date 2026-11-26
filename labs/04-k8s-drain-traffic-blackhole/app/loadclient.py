from __future__ import annotations

import concurrent.futures
import errno
import http.client
import json
import math
import os
import threading
import time
from typing import Any
from urllib.parse import urlparse

TARGET_URL = os.getenv("TARGET_URL", "http://api:8080/work?ms=300")
RPS = max(1, int(os.getenv("RPS", "20")))
DURATION_S = max(1, int(os.getenv("DURATION_S", "75")))
TIMEOUT_S = float(os.getenv("TIMEOUT_S", "3.0"))
MAX_WORKERS = max(8, int(os.getenv("MAX_WORKERS", "64")))


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    if len(sorted_vals) == 1:
        return round(sorted_vals[0], 2)
    idx = (pct / 100.0) * (len(sorted_vals) - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    val = sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo)
    return round(val, 2)


def _classify_exception(exc: Exception) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionRefusedError):
        return "conn_refused"
    if isinstance(
        exc,
        (
            ConnectionResetError,
            BrokenPipeError,
            http.client.RemoteDisconnected,
            http.client.IncompleteRead,
            http.client.BadStatusLine,
        ),
    ):
        return "conn_reset"
    if isinstance(exc, OSError):
        if exc.errno == errno.ECONNREFUSED:
            return "conn_refused"
        if exc.errno in (errno.ECONNRESET, errno.EPIPE):
            return "conn_reset"
    return "other_err"


def _send_one(host: str, port: int, path: str, timeout_s: float) -> tuple[str, float]:
    t0 = time.perf_counter()
    conn = http.client.HTTPConnection(host, port, timeout=timeout_s)
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        resp = conn.getresponse()
        resp.read()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        if 200 <= resp.status < 300:
            return "ok", elapsed_ms
        if 500 <= resp.status < 600:
            return "http_5xx", elapsed_ms
        return "http_other", elapsed_ms
    except (TimeoutError, OSError, http.client.HTTPException) as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return _classify_exception(exc), elapsed_ms
    finally:
        conn.close()


def main() -> None:
    parsed = urlparse(TARGET_URL)
    host = parsed.hostname or "api"
    port = parsed.port or 80
    path = parsed.path or "/work"
    if parsed.query:
        path = f"{path}?{parsed.query}"

    buckets_lock = threading.Lock()
    buckets: dict[int, dict[str, Any]] = {}

    def _record(sec: int, outcome: str, latency_ms: float) -> None:
        with buckets_lock:
            entry = buckets.setdefault(
                sec,
                {
                    "ok": 0,
                    "http_5xx": 0,
                    "http_other": 0,
                    "conn_refused": 0,
                    "conn_reset": 0,
                    "timeout": 0,
                    "other_err": 0,
                    "latencies_ms": [],
                },
            )
            entry[outcome] += 1
            entry["latencies_ms"].append(latency_ms)

    def _fire(sec: int) -> None:
        outcome, latency_ms = _send_one(host, port, path, TIMEOUT_S)
        _record(sec, outcome, latency_ms)

    start = time.monotonic()
    interval = 1.0 / float(RPS)

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures: list[concurrent.futures.Future[None]] = []
        total_slots = DURATION_S * RPS
        for slot in range(total_slots):
            target_t = start + slot * interval
            now = time.monotonic()
            if target_t > now:
                time.sleep(target_t - now)
            sec = min(DURATION_S - 1, slot // RPS)
            futures.append(pool.submit(_fire, sec))

        for fut in futures:
            fut.result()

    for sec in range(DURATION_S):
        entry = buckets.get(
            sec,
            {
                "ok": 0,
                "http_5xx": 0,
                "http_other": 0,
                "conn_refused": 0,
                "conn_reset": 0,
                "timeout": 0,
                "other_err": 0,
                "latencies_ms": [],
            },
        )
        lats = sorted(entry["latencies_ms"])
        total = (
            entry["ok"]
            + entry["http_5xx"]
            + entry["http_other"]
            + entry["conn_refused"]
            + entry["conn_reset"]
            + entry["timeout"]
            + entry["other_err"]
        )
        row = {
            "sec": sec,
            "total": total,
            "ok": entry["ok"],
            "http_5xx": entry["http_5xx"],
            "http_other": entry["http_other"],
            "conn_refused": entry["conn_refused"],
            "conn_reset": entry["conn_reset"],
            "timeout": entry["timeout"],
            "other_err": entry["other_err"],
            "p50_ms": _percentile(lats, 50.0),
            "p95_ms": _percentile(lats, 95.0),
        }
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
