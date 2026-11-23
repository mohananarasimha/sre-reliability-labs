from __future__ import annotations

import asyncio
import math
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

import httpx


def compute_percentile(sorted_values: list[float], percentile: float) -> float:
    """Compute linear-interpolated percentile on a pre-sorted list of floats."""
    if not 0.0 <= percentile <= 100.0:
        raise ValueError(f"percentile must be between 0 and 100, got {percentile}")
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])

    idx = (percentile / 100.0) * (len(sorted_values) - 1)
    lower = math.floor(idx)
    upper = math.ceil(idx)
    if lower == upper:
        return float(sorted_values[lower])

    fraction = idx - lower
    return float(sorted_values[lower] + (sorted_values[upper] - sorted_values[lower]) * fraction)


@dataclass(slots=True)
class LoadConfig:
    """Configuration for a single load-generation run."""

    url: str
    concurrency: int = 10
    duration_s: float = 10.0
    rps: float | None = None
    timeout_s: float = 5.0
    method: str = "GET"
    headers: dict[str, str] = field(default_factory=dict)
    stream_sse: bool = False

    def __post_init__(self) -> None:
        if self.concurrency < 1:
            raise ValueError("concurrency must be >= 1")
        if self.duration_s <= 0:
            raise ValueError("duration_s must be > 0")
        if self.timeout_s <= 0:
            raise ValueError("timeout_s must be > 0")
        if self.rps is not None and self.rps <= 0:
            raise ValueError("rps must be > 0 when specified")
        self.method = self.method.upper()


@dataclass(slots=True)
class LatencyStats:
    """Response latency distribution in milliseconds (completed HTTP responses)."""

    min_ms: float
    mean_ms: float
    p50_ms: float
    p95_ms: float
    p99_ms: float
    max_ms: float

    @classmethod
    def from_samples(cls, samples_ms: list[float]) -> LatencyStats:
        if not samples_ms:
            return cls(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
        ordered = sorted(samples_ms)
        return cls(
            min_ms=ordered[0],
            mean_ms=sum(ordered) / len(ordered),
            p50_ms=compute_percentile(ordered, 50.0),
            p95_ms=compute_percentile(ordered, 95.0),
            p99_ms=compute_percentile(ordered, 99.0),
            max_ms=ordered[-1],
        )

    def to_dict(self) -> dict[str, float]:
        return {
            "min_ms": round(self.min_ms, 3),
            "mean_ms": round(self.mean_ms, 3),
            "p50_ms": round(self.p50_ms, 3),
            "p95_ms": round(self.p95_ms, 3),
            "p99_ms": round(self.p99_ms, 3),
            "max_ms": round(self.max_ms, 3),
        }


@dataclass(slots=True)
class LoadResult:
    """Deterministic evidence payload produced by a load run."""

    url: str
    method: str
    concurrency: int
    duration_requested_s: float
    duration_actual_s: float
    target_rps: float | None
    total_requests: int
    success_requests: int
    error_requests: int
    error_rate: float
    achieved_rps: float
    status_codes: dict[str, int]
    transport_errors: dict[str, int]
    latency_ms: LatencyStats

    def to_dict(self) -> dict[str, Any]:
        return {
            "url": self.url,
            "method": self.method,
            "concurrency": self.concurrency,
            "duration_requested_s": round(self.duration_requested_s, 3),
            "duration_actual_s": round(self.duration_actual_s, 3),
            "target_rps": self.target_rps,
            "total_requests": self.total_requests,
            "success_requests": self.success_requests,
            "error_requests": self.error_requests,
            "error_rate": round(self.error_rate, 6),
            "achieved_rps": round(self.achieved_rps, 3),
            "status_codes": dict(sorted(self.status_codes.items())),
            "transport_errors": dict(sorted(self.transport_errors.items())),
            "latency_ms": self.latency_ms.to_dict(),
        }


class _RatePacer:
    """Shared monotonic slot pacer across async workers."""

    def __init__(self, rps: float, start_monotonic: float) -> None:
        self._interval = 1.0 / rps
        self._start = start_monotonic
        self._slot = 0
        self._lock = asyncio.Lock()

    async def wait_for_slot(self, deadline: float) -> bool:
        async with self._lock:
            target = self._start + (self._slot * self._interval)
            self._slot += 1

        if target >= deadline:
            return False

        delay = target - time.monotonic()
        if delay > 0:
            await asyncio.sleep(delay)

        return time.monotonic() < deadline


def _classify_transport_error(exc: Exception) -> str:
    if isinstance(exc, httpx.TimeoutException):
        return "timeout"
    if isinstance(exc, httpx.ConnectError):
        return "connect_error"
    if isinstance(exc, httpx.ProtocolError):
        return "protocol_error"
    return "other_error"


async def _execute_request(
    client: httpx.AsyncClient,
    config: LoadConfig,
) -> tuple[int | None, str | None, float]:
    """Execute one HTTP request (or SSE stream hook) and return (status, err_kind, latency_ms)."""
    t0 = time.perf_counter()
    try:
        if config.stream_sse:
            async with client.stream(
                config.method,
                config.url,
                headers=config.headers,
            ) as response:
                async for _ in response.aiter_bytes():
                    pass
                elapsed_ms = (time.perf_counter() - t0) * 1000.0
                return response.status_code, None, elapsed_ms

        response = await client.request(
            config.method,
            config.url,
            headers=config.headers,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return response.status_code, None, elapsed_ms
    except httpx.HTTPError as exc:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return None, _classify_transport_error(exc), elapsed_ms
    except Exception:
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        return None, "other_error", elapsed_ms


async def run_load(
    config: LoadConfig,
    transport: httpx.AsyncBaseTransport | None = None,
) -> LoadResult:
    """Run an asynchronous HTTP load test according to ``config``."""
    status_counts: Counter[str] = Counter()
    transport_errors: Counter[str] = Counter()
    latencies_ms: list[float] = []

    timeout = httpx.Timeout(config.timeout_s)
    client_kwargs: dict[str, Any] = {"timeout": timeout}
    if transport is not None:
        client_kwargs["transport"] = transport
    else:
        client_kwargs["limits"] = httpx.Limits(
            max_connections=max(config.concurrency * 2, 20),
            max_keepalive_connections=config.concurrency,
        )

    start_time = time.monotonic()
    deadline = start_time + config.duration_s
    pacer = _RatePacer(config.rps, start_time) if config.rps is not None else None

    async with httpx.AsyncClient(**client_kwargs) as client:

        async def worker() -> None:
            while time.monotonic() < deadline:
                if pacer is not None:
                    allowed = await pacer.wait_for_slot(deadline)
                    if not allowed:
                        break
                status, err_kind, elapsed_ms = await _execute_request(client, config)
                if status is not None:
                    status_counts[str(status)] += 1
                    latencies_ms.append(elapsed_ms)
                elif err_kind is not None:
                    transport_errors[err_kind] += 1

        await asyncio.gather(*(worker() for _ in range(config.concurrency)))

    actual_duration = max(time.monotonic() - start_time, 1e-6)
    total_requests = sum(status_counts.values()) + sum(transport_errors.values())
    success_requests = sum(
        count for code, count in status_counts.items() if 200 <= int(code) < 400
    )
    error_requests = total_requests - success_requests
    error_rate = (error_requests / total_requests) if total_requests > 0 else 0.0
    achieved_rps = total_requests / actual_duration

    return LoadResult(
        url=config.url,
        method=config.method,
        concurrency=config.concurrency,
        duration_requested_s=config.duration_s,
        duration_actual_s=actual_duration,
        target_rps=config.rps,
        total_requests=total_requests,
        success_requests=success_requests,
        error_requests=error_requests,
        error_rate=error_rate,
        achieved_rps=achieved_rps,
        status_codes=dict(status_counts),
        transport_errors=dict(transport_errors),
        latency_ms=LatencyStats.from_samples(latencies_ms),
    )
