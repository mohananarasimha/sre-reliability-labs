from __future__ import annotations

import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

PORT = int(os.getenv("PORT", "8080"))
LAB_PROFILE = os.getenv("LAB_PROFILE", "unknown")
SHUTDOWN_MODE = os.getenv("SHUTDOWN_MODE", "immediate").lower()
DEFAULT_WORK_MS = max(0, int(os.getenv("WORK_MS", "300")))
DRAIN_TIMEOUT_S = max(1.0, float(os.getenv("DRAIN_TIMEOUT_S", "8.0")))

_shutting_down = threading.Event()
_in_flight_lock = threading.Lock()
_in_flight_cond = threading.Condition(_in_flight_lock)
_in_flight = 0


def _parse_work_ms(query_string: str) -> int:
    params = parse_qs(query_string)
    raw = params.get("ms", [None])[0]
    if raw is None:
        return DEFAULT_WORK_MS
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_WORK_MS
    return max(0, min(parsed, 5_000))


class DrainApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/livez":
            self._send_json(200, {"status": "alive", "profile": LAB_PROFILE})
            return

        if path == "/readyz":
            if _shutting_down.is_set():
                self._send_json(503, {"status": "draining", "profile": LAB_PROFILE})
                return
            self._send_json(200, {"status": "ready", "profile": LAB_PROFILE})
            return

        if path == "/work":
            global _in_flight
            with _in_flight_lock:
                _in_flight += 1
                active = _in_flight
            try:
                work_ms = _parse_work_ms(parsed.query)
                t0 = time.perf_counter()
                if work_ms > 0:
                    time.sleep(work_ms / 1000.0)
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "profile": LAB_PROFILE,
                        "shutdown_mode": SHUTDOWN_MODE,
                        "work_ms": work_ms,
                        "elapsed_ms": round(elapsed_ms, 2),
                        "in_flight": active,
                    },
                )
            finally:
                with _in_flight_cond:
                    _in_flight -= 1
                    if _in_flight == 0:
                        _in_flight_cond.notify_all()
            return

        self._send_json(404, {"error": "not_found", "path": path})


def main() -> None:
    bind_host = os.getenv("BIND_HOST", "")
    server = ThreadingHTTPServer((bind_host, PORT), DrainApiHandler)
    server.daemon_threads = True
    drained = threading.Event()

    def _graceful_shutdown() -> None:
        _shutting_down.set()
        # Stop accepting new connections; keep existing handler threads running.
        server.shutdown()
        deadline = time.monotonic() + DRAIN_TIMEOUT_S
        with _in_flight_cond:
            while _in_flight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                _in_flight_cond.wait(timeout=remaining)
        drained.set()

    def _handle_sigterm(_signum: int, _frame: Any) -> None:
        if SHUTDOWN_MODE == "immediate":
            # Abandon listener and in-flight requests immediately on SIGTERM.
            os._exit(0)
        threading.Thread(target=_graceful_shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # serve_forever() returns as soon as shutdown() is called; handler threads are
        # daemons, so exiting here without waiting would abandon in-flight requests.
        if _shutting_down.is_set():
            drained.wait(timeout=DRAIN_TIMEOUT_S + 1.0)
        server.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
