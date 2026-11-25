from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

PORT = int(os.getenv("PORT", "8080"))
INITIAL_DELAY_MS = max(0, int(os.getenv("INITIAL_DELAY_MS", "10")))

_state_lock = threading.Lock()
_delay_ms = INITIAL_DELAY_MS


def get_delay_ms() -> int:
    with _state_lock:
        return _delay_ms


def set_delay_ms(new_ms: int) -> int:
    global _delay_ms
    clamped = max(0, min(new_ms, 15_000))
    with _state_lock:
        _delay_ms = clamped
        return _delay_ms


class DownstreamHandler(BaseHTTPRequestHandler):
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
        parsed = urlparse(self.path)
        if parsed.path == "/healthz":
            self._send_json(200, {"status": "ok", "delay_ms": get_delay_ms()})
            return

        if parsed.path == "/admin/latency":
            self._send_json(200, {"delay_ms": get_delay_ms()})
            return

        if parsed.path == "/query":
            delay_ms = get_delay_ms()
            if delay_ms > 0:
                time.sleep(delay_ms / 1000.0)
            self._send_json(200, {"status": "ok", "delay_ms": delay_ms})
            return

        self._send_json(404, {"error": "not_found", "path": parsed.path})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/admin/latency":
            self._send_json(404, {"error": "not_found", "path": parsed.path})
            return

        params = parse_qs(parsed.query)
        raw_ms = params.get("ms", [None])[0]
        if raw_ms is None:
            content_length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(content_length) if content_length > 0 else b"{}"
            try:
                body_doc = json.loads(raw_body.decode("utf-8"))
                raw_ms = str(body_doc.get("delay_ms", ""))
            except (ValueError, UnicodeDecodeError):
                raw_ms = ""

        try:
            new_ms = int(raw_ms)
        except (TypeError, ValueError):
            self._send_json(400, {"error": "invalid_delay_ms"})
            return

        updated = set_delay_ms(new_ms)
        self._send_json(200, {"status": "updated", "delay_ms": updated})


def main() -> None:
    bind_host = os.getenv("BIND_HOST", "")
    server = ThreadingHTTPServer((bind_host, PORT), DownstreamHandler)
    server.daemon_threads = True
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
