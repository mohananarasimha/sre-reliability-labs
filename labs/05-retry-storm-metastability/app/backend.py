"""Lab 05 backend: fixed-capacity service with a bounded waiting queue.

Capacity is CONCURRENCY / SERVICE_MS. Requests beyond the worker pool wait in a
queue of at most MAX_QUEUE; above that the backend answers 503 immediately.
Admitted requests are always processed to completion, even if the caller has
already given up - like most real servers, this one cannot see abandonment.

POST /admin/slow?ms=N&seconds=S adds N ms to every request that *starts* service
in the next S seconds, then reverts on its own (no external timer needed).
"""

from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

PORT = int(os.getenv("PORT", "8080"))
SERVICE_MS = float(os.getenv("SERVICE_MS", "20"))
CONCURRENCY = int(os.getenv("CONCURRENCY", "4"))
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "500"))

_workers = threading.BoundedSemaphore(CONCURRENCY)
_lock = threading.Lock()
_stats = {
    "waiting": 0,
    "in_service": 0,
    "admitted_total": 0,
    "rejected_total": 0,
    "completed_total": 0,
    "write_failed_total": 0,
}
_slow = {"extra_ms": 0.0, "until": 0.0}


def _service_seconds() -> float:
    with _lock:
        extra = _slow["extra_ms"] if time.monotonic() < _slow["until"] else 0.0
    return (SERVICE_MS + extra) / 1000.0


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lab05-backend"
    sys_version = ""

    def log_message(self, fmt: str, *args: object) -> None:
        return

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json(200, {"status": "ok"})
        elif path == "/stats":
            with _lock:
                snap = dict(_stats)
                snap["slow_active"] = time.monotonic() < _slow["until"]
            snap["capacity_rps"] = CONCURRENCY * 1000.0 / SERVICE_MS
            snap["max_queue"] = MAX_QUEUE
            self._json(200, snap)
        elif path == "/work":
            self._work()
        else:
            self._json(404, {"error": "not found"})

    def _work(self) -> None:
        with _lock:
            if _stats["waiting"] >= MAX_QUEUE:
                _stats["rejected_total"] += 1
                admitted = False
            else:
                _stats["waiting"] += 1
                _stats["admitted_total"] += 1
                admitted = True
        if not admitted:
            self._json(503, {"error": "queue full"})
            return

        with _workers:
            with _lock:
                _stats["waiting"] -= 1
                _stats["in_service"] += 1
            try:
                time.sleep(_service_seconds())
            finally:
                with _lock:
                    _stats["in_service"] -= 1
                    _stats["completed_total"] += 1
        try:
            self._json(200, {"ok": True})
        except (BrokenPipeError, ConnectionResetError):
            with _lock:
                _stats["write_failed_total"] += 1

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path != "/admin/slow":
            self._json(404, {"error": "not found"})
            return
        qs = parse_qs(url.query)
        try:
            extra_ms = float(qs.get("ms", ["0"])[0])
            seconds = float(qs.get("seconds", ["0"])[0])
        except ValueError:
            self._json(400, {"error": "ms and seconds must be numbers"})
            return
        if not (0 <= extra_ms <= 5000 and 0 <= seconds <= 120):
            self._json(400, {"error": "ms must be 0..5000 and seconds 0..120"})
            return
        with _lock:
            _slow["extra_ms"] = extra_ms
            _slow["until"] = time.monotonic() + seconds
        self._json(200, {"extra_ms": extra_ms, "seconds": seconds})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 1024


if __name__ == "__main__":
    print(
        f"backend: capacity={CONCURRENCY * 1000.0 / SERVICE_MS:.0f} rps "
        f"(concurrency={CONCURRENCY}, service_ms={SERVICE_MS}), max_queue={MAX_QUEUE}",
        flush=True,
    )
    Server((os.getenv("BIND_HOST", ""), PORT), Handler).serve_forever()
