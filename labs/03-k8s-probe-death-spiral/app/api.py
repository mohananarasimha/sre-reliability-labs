from __future__ import annotations

import http.client
import json
import os
import signal
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

PORT = int(os.getenv("PORT", "8080"))
LAB_PROFILE = os.getenv("LAB_PROFILE", "unknown")
WARMUP_SECONDS = max(0.0, float(os.getenv("WARMUP_SECONDS", "12")))
DOWNSTREAM_HOST = os.getenv("DOWNSTREAM_HOST", "downstream")
DOWNSTREAM_PORT = int(os.getenv("DOWNSTREAM_PORT", "8080"))
WORK_TIMEOUT_S = float(os.getenv("WORK_TIMEOUT_S", "1.5"))
DEEP_HEALTH_TIMEOUT_S = float(os.getenv("DEEP_HEALTH_TIMEOUT_S", "0.9"))

_start_monotonic = time.monotonic()
_warmed_up = threading.Event()


def _run_warmup() -> None:
    if WARMUP_SECONDS > 0:
        time.sleep(WARMUP_SECONDS)
    _warmed_up.set()


def _call_downstream(timeout_s: float) -> tuple[bool, int, float, str | None]:
    t0 = time.perf_counter()
    conn = http.client.HTTPConnection(
        DOWNSTREAM_HOST,
        DOWNSTREAM_PORT,
        timeout=timeout_s,
    )
    try:
        conn.request("GET", "/query")
        resp = conn.getresponse()
        resp.read()
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return resp.status == 200, resp.status, elapsed_ms, None
    except TimeoutError:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return False, 504, elapsed_ms, "downstream_timeout"
    except (OSError, http.client.HTTPException) as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return False, 502, elapsed_ms, f"downstream_error:{type(exc).__name__}"
    finally:
        conn.close()


class ApiHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _send_json(self, code: int, payload: dict[str, Any]) -> None:
        body = (json.dumps(payload) + "\n").encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = urlparse(self.path).path

        if path == "/livez":
            self._send_json(
                200,
                {
                    "status": "alive",
                    "profile": LAB_PROFILE,
                    "uptime_s": round(time.monotonic() - _start_monotonic, 2),
                },
            )
            return

        if path == "/readyz":
            if not _warmed_up.is_set():
                self._send_json(
                    503,
                    {
                        "status": "warming_up",
                        "profile": LAB_PROFILE,
                        "uptime_s": round(time.monotonic() - _start_monotonic, 2),
                    },
                )
                return
            self._send_json(200, {"status": "ready", "profile": LAB_PROFILE})
            return

        if path == "/healthz/deep":
            if not _warmed_up.is_set():
                self._send_json(
                    503,
                    {
                        "status": "warming_up",
                        "profile": LAB_PROFILE,
                        "uptime_s": round(time.monotonic() - _start_monotonic, 2),
                    },
                )
                return
            ok, status_code, elapsed_ms, err = _call_downstream(DEEP_HEALTH_TIMEOUT_S)
            if ok:
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "profile": LAB_PROFILE,
                        "downstream_ms": round(elapsed_ms, 2),
                    },
                )
                return
            self._send_json(
                503,
                {
                    "status": "unhealthy",
                    "profile": LAB_PROFILE,
                    "downstream_status": status_code,
                    "downstream_ms": round(elapsed_ms, 2),
                    "error": err,
                },
            )
            return

        if path == "/work":
            if not _warmed_up.is_set():
                self._send_json(503, {"error": "warming_up", "profile": LAB_PROFILE})
                return
            ok, status_code, elapsed_ms, err = _call_downstream(WORK_TIMEOUT_S)
            if ok:
                self._send_json(
                    200,
                    {
                        "status": "ok",
                        "profile": LAB_PROFILE,
                        "downstream_ms": round(elapsed_ms, 2),
                    },
                )
                return
            self._send_json(
                status_code,
                {
                    "status": "degraded",
                    "profile": LAB_PROFILE,
                    "downstream_ms": round(elapsed_ms, 2),
                    "error": err,
                },
            )
            return

        self._send_json(404, {"error": "not_found", "path": path})


def main() -> None:
    threading.Thread(target=_run_warmup, daemon=True).start()

    bind_host = os.getenv("BIND_HOST", "")
    server = ThreadingHTTPServer((bind_host, PORT), ApiHandler)
    server.daemon_threads = True

    def _handle_sigterm(_signum: int, _frame: Any) -> None:
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _handle_sigterm)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        sys.exit(0)


if __name__ == "__main__":
    main()
