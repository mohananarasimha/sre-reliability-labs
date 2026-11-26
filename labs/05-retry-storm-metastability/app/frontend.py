"""Lab 05 frontend: calls the backend with a configurable retry policy.

RETRY_POLICY is the single experimental variable:
  naive     retry immediately, up to MAX_RETRIES times
  backoff   exponential backoff with full jitter, up to MAX_RETRIES times
  budgeted  backoff + a retry budget: every original request deposits
            BUDGET_RATIO tokens, every retry spends one (bucket capped at
            BUDGET_MAX), so retries cannot exceed ~BUDGET_RATIO of traffic

Every policy uses the same per-attempt timeout and the same MAX_RETRIES.
"""

from __future__ import annotations

import http.client
import json
import os
import random
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

PORT = int(os.getenv("PORT", "8080"))
BACKEND = urlparse(os.getenv("BACKEND_URL", "http://backend:8080/work"))
RETRY_POLICY = os.getenv("RETRY_POLICY", "naive")
ATTEMPT_TIMEOUT_S = float(os.getenv("ATTEMPT_TIMEOUT_S", "0.2"))
MAX_RETRIES = int(os.getenv("MAX_RETRIES", "3"))
BACKOFF_BASE_S = float(os.getenv("BACKOFF_BASE_S", "0.05"))
BACKOFF_CAP_S = float(os.getenv("BACKOFF_CAP_S", "1.0"))
BUDGET_RATIO = float(os.getenv("BUDGET_RATIO", "0.1"))
BUDGET_MAX = float(os.getenv("BUDGET_MAX", "10"))

if RETRY_POLICY not in ("naive", "backoff", "budgeted"):
    raise SystemExit(f"RETRY_POLICY must be naive|backoff|budgeted: {RETRY_POLICY!r}")

_lock = threading.Lock()
_stats = {
    "requests_total": 0,
    "attempts_total": 0,
    "retries_total": 0,
    "budget_denied_total": 0,
    "ok_total": 0,
    "failed_total": 0,
    "attempt_timeout_total": 0,
    "attempt_503_total": 0,
    "attempt_conn_error_total": 0,
}


class RetryBudget:
    def __init__(self, ratio: float, max_tokens: float) -> None:
        self._ratio = ratio
        self._max = max_tokens
        self._tokens = max_tokens
        self._lock = threading.Lock()

    def deposit(self) -> None:
        with self._lock:
            self._tokens = min(self._max, self._tokens + self._ratio)

    def try_spend(self) -> bool:
        with self._lock:
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return True
            return False

    def tokens(self) -> float:
        with self._lock:
            return self._tokens


_budget = RetryBudget(BUDGET_RATIO, BUDGET_MAX)


def _count(key: str) -> None:
    with _lock:
        _stats[key] += 1


def _attempt() -> str:
    conn = http.client.HTTPConnection(
        BACKEND.hostname, BACKEND.port or 80, timeout=ATTEMPT_TIMEOUT_S
    )
    try:
        conn.request("GET", BACKEND.path or "/", headers={"Connection": "close"})
        resp = conn.getresponse()
        resp.read()
        if resp.status == 200:
            return "ok"
        return "503" if resp.status == 503 else "error"
    except TimeoutError:
        return "timeout"
    except OSError:
        return "conn_error"
    finally:
        conn.close()


def call_backend() -> tuple[bool, str]:
    _count("requests_total")
    if RETRY_POLICY == "budgeted":
        _budget.deposit()
    outcome = "error"
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            if RETRY_POLICY == "budgeted" and not _budget.try_spend():
                _count("budget_denied_total")
                break
            if RETRY_POLICY in ("backoff", "budgeted"):
                ceiling = min(BACKOFF_CAP_S, BACKOFF_BASE_S * (2 ** (attempt - 1)))
                time.sleep(random.uniform(0.0, ceiling))  # noqa: S311 - jitter, not crypto
            _count("retries_total")
        _count("attempts_total")
        outcome = _attempt()
        if outcome == "ok":
            _count("ok_total")
            return True, outcome
        if outcome == "timeout":
            _count("attempt_timeout_total")
        elif outcome == "503":
            _count("attempt_503_total")
        else:
            _count("attempt_conn_error_total")
    _count("failed_total")
    return False, outcome


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "lab05-frontend"
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
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass
        self.close_connection = True

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/healthz":
            self._json(200, {"status": "ok", "retry_policy": RETRY_POLICY})
        elif path == "/stats":
            with _lock:
                snap = dict(_stats)
            snap["retry_policy"] = RETRY_POLICY
            snap["budget_tokens"] = round(_budget.tokens(), 2)
            self._json(200, snap)
        elif path == "/work":
            ok, last = call_backend()
            if ok:
                self._json(200, {"ok": True})
            else:
                self._json(504 if last == "timeout" else 503, {"error": last})
        else:
            self._json(404, {"error": "not found"})


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 1024


if __name__ == "__main__":
    print(
        f"frontend: policy={RETRY_POLICY} attempt_timeout={ATTEMPT_TIMEOUT_S}s "
        f"max_retries={MAX_RETRIES}",
        flush=True,
    )
    Server((os.getenv("BIND_HOST", ""), PORT), Handler).serve_forever()
