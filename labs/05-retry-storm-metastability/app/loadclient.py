"""Open-loop client for Lab 05.

Sends RPS requests/second on a fixed schedule for DURATION_S seconds. Results
are bucketed by the second each request was *scheduled*, so a slow response
never delays the next send. The first output line carries the wall-clock start
time so the runner can align client seconds with the fault-injection time.

late_sends counts requests that left more than 50 ms after their scheduled
time (client-side saturation). If it is non-zero, the run is not open-loop.
"""

from __future__ import annotations

import concurrent.futures
import http.client
import json
import math
import os
import threading
import time
from urllib.parse import urlparse

TARGET_URL = os.getenv("TARGET_URL", "http://frontend:8080/work")
RPS = max(1, int(os.getenv("RPS", "120")))
DURATION_S = max(1, int(os.getenv("DURATION_S", "90")))
TIMEOUT_S = float(os.getenv("TIMEOUT_S", "2.0"))
MAX_WORKERS = max(8, int(os.getenv("MAX_WORKERS", "400")))
LATE_THRESHOLD_S = 0.05

OUTCOMES = ("ok", "http_503", "http_504", "http_other", "timeout", "conn_error")


def _percentile(sorted_vals: list[float], pct: float) -> float:
    if not sorted_vals:
        return 0.0
    idx = (pct / 100.0) * (len(sorted_vals) - 1)
    lo, hi = math.floor(idx), math.ceil(idx)
    return round(sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (idx - lo), 1)


def _send(host: str, port: int, path: str) -> tuple[str, float]:
    t0 = time.perf_counter()
    conn = http.client.HTTPConnection(host, port, timeout=TIMEOUT_S)
    try:
        conn.request("GET", path, headers={"Connection": "close"})
        resp = conn.getresponse()
        resp.read()
        status = resp.status
        if status == 200:
            outcome = "ok"
        elif status in (503, 504):
            outcome = f"http_{status}"
        else:
            outcome = "http_other"
    except TimeoutError:
        outcome = "timeout"
    except (OSError, http.client.HTTPException):
        outcome = "conn_error"
    finally:
        conn.close()
    return outcome, (time.perf_counter() - t0) * 1000.0


def main() -> None:
    url = urlparse(TARGET_URL)
    host, port, path = url.hostname or "frontend", url.port or 80, url.path or "/"

    lock = threading.Lock()
    buckets = [
        {k: 0 for k in OUTCOMES} | {"lat": [], "late": 0} for _ in range(DURATION_S)
    ]

    def fire(sec: int, scheduled: float) -> None:
        late = time.monotonic() - scheduled > LATE_THRESHOLD_S
        outcome, ms = _send(host, port, path)
        with lock:
            b = buckets[sec]
            b[outcome] += 1
            b["lat"].append(ms)
            b["late"] += int(late)

    start_mono = time.monotonic()
    header = {"start_epoch": time.time(), "rps": RPS, "duration_s": DURATION_S}
    print(json.dumps(header), flush=True)
    interval = 1.0 / RPS
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = []
        for slot in range(DURATION_S * RPS):
            scheduled = start_mono + slot * interval
            delay = scheduled - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            futures.append(pool.submit(fire, slot // RPS, scheduled))
        for fut in futures:
            fut.result()

    for sec, b in enumerate(buckets):
        lat = sorted(b.pop("lat"))
        row = {"sec": sec, "total": len(lat)} | b
        row["p50_ms"] = _percentile(lat, 50.0)
        row["p99_ms"] = _percentile(lat, 99.0)
        print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
