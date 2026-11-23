from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from loadgen.runner import LoadConfig, LoadResult, run_load

app = typer.Typer(
    no_args_is_help=True,
    add_completion=False,
    help="Async HTTP load generator for sre-reliability-labs.",
)
console = Console()


@app.callback()
def main() -> None:
    """Async HTTP load generator for sre-reliability-labs."""


def _parse_headers(raw_headers: list[str] | None) -> dict[str, str]:
    parsed: dict[str, str] = {}
    if not raw_headers:
        return parsed
    for item in raw_headers:
        if ":" not in item:
            raise typer.BadParameter(f"Header must be in 'Key: Value' format, got: {item!r}")
        key, value = item.split(":", 1)
        key = key.strip()
        if not key:
            raise typer.BadParameter(f"Header name cannot be empty in: {item!r}")
        parsed[key] = value.strip()
    return parsed


def _render_summary(result: LoadResult) -> None:
    table = Table(title="Load Test Summary", show_header=True, header_style="bold")
    table.add_column("Metric", style="cyan")
    table.add_column("Value", justify="right")

    table.add_row("Target", f"{result.method} {result.url}")
    table.add_row("Concurrency", str(result.concurrency))
    table.add_row("Duration (s)", f"{result.duration_actual_s:.2f}")
    table.add_row("Total Requests", str(result.total_requests))
    table.add_row("Success (2xx/3xx)", str(result.success_requests))
    table.add_row("Errors", str(result.error_requests))
    table.add_row("Error Rate", f"{result.error_rate * 100:.2f}%")
    table.add_row("Achieved RPS", f"{result.achieved_rps:.2f}")
    table.add_row(
        "Latency ms (p50/p95/p99)",
        (
            f"{result.latency_ms.p50_ms:.2f} / "
            f"{result.latency_ms.p95_ms:.2f} / "
            f"{result.latency_ms.p99_ms:.2f}"
        ),
    )
    table.add_row(
        "Latency ms (min/mean/max)",
        (
            f"{result.latency_ms.min_ms:.2f} / "
            f"{result.latency_ms.mean_ms:.2f} / "
            f"{result.latency_ms.max_ms:.2f}"
        ),
    )

    if result.status_codes:
        codes_str = ", ".join(f"{k}:{v}" for k, v in sorted(result.status_codes.items()))
        table.add_row("Status Codes", codes_str)

    if result.transport_errors:
        errs_str = ", ".join(f"{k}:{v}" for k, v in sorted(result.transport_errors.items()))
        table.add_row("Transport Errors", errs_str)

    console.print(table)


@app.command("run")
def run_command(
    url: Annotated[str, typer.Option("--url", help="Target HTTP URL.")],
    concurrency: Annotated[
        int, typer.Option("--concurrency", min=1, help="Concurrent async workers.")
    ] = 10,
    duration: Annotated[
        float, typer.Option("--duration", help="Run duration in seconds.")
    ] = 10.0,
    rps: Annotated[
        float | None, typer.Option("--rps", help="Optional target requests per second.")
    ] = None,
    timeout: Annotated[
        float, typer.Option("--timeout", help="Per-request timeout in seconds.")
    ] = 5.0,
    method: Annotated[str, typer.Option("--method", help="HTTP method.")] = "GET",
    headers: Annotated[
        list[str] | None,
        typer.Option(
            "--headers",
            "--header",
            help="HTTP header in 'Key: Value' format (repeatable).",
        ),
    ] = None,
    stream_sse: Annotated[
        bool,
        typer.Option(
            "--stream-sse",
            help="Read response body as a stream (SSE extension hook).",
        ),
    ] = False,
    json_out: Annotated[
        Path | None,
        typer.Option(
            "--json-out",
            help="Write structured JSON report to this path.",
        ),
    ] = None,
    max_error_rate: Annotated[
        float,
        typer.Option(
            "--max-error-rate",
            min=0.0,
            max=1.0,
            help="Maximum allowed error rate (0.0 to 1.0) for exit code 0.",
        ),
    ] = 0.0,
) -> None:
    """Execute a controlled HTTP load test and report latency/error evidence."""
    parsed_headers = _parse_headers(headers)
    try:
        config = LoadConfig(
            url=url,
            concurrency=concurrency,
            duration_s=duration,
            rps=rps,
            timeout_s=timeout,
            method=method,
            headers=parsed_headers,
            stream_sse=stream_sse,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

    result = asyncio.run(run_load(config))
    _render_summary(result)

    if json_out is not None:
        json_out.parent.mkdir(parents=True, exist_ok=True)
        json_out.write_text(json.dumps(result.to_dict(), indent=2) + "\n", encoding="utf-8")

    if result.total_requests == 0 or result.error_rate > max_error_rate:
        raise typer.Exit(code=1)
