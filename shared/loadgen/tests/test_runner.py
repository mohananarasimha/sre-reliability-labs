from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

from loadgen.cli import app
from loadgen.runner import LatencyStats, LoadConfig, compute_percentile, run_load


@pytest.mark.asyncio
async def test_healthy_200_responses() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers.get("X-Lab") == "baseline"
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    config = LoadConfig(
        url="http://service.local/health",
        concurrency=2,
        duration_s=0.15,
        rps=40.0,
        headers={"X-Lab": "baseline"},
    )

    result = await run_load(config, transport=transport)

    assert result.total_requests >= 2
    assert result.success_requests == result.total_requests
    assert result.error_requests == 0
    assert result.error_rate == 0.0
    assert result.status_codes == {"200": result.total_requests}
    assert result.transport_errors == {}
    assert result.latency_ms.min_ms >= 0.0
    assert result.latency_ms.p50_ms <= result.latency_ms.p95_ms <= result.latency_ms.p99_ms
    assert result.achieved_rps > 0.0
    assert result.target_rate_sustained is True
    assert result.to_dict()["target_rate_sustained"] is True


@pytest.mark.asyncio
async def test_target_rate_sustained_none_without_rps() -> None:
    transport = httpx.MockTransport(lambda _req: httpx.Response(200, text="ok"))
    config = LoadConfig(
        url="http://service.local/health",
        concurrency=1,
        duration_s=0.05,
        rps=None,
    )
    result = await run_load(config, transport=transport)
    assert result.target_rps is None
    assert result.target_rate_sustained is None
    assert result.to_dict()["target_rate_sustained"] is None


def test_under_delivered_rps_warns_coordinated_omission(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    import asyncio

    async def slow_handler(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.06)
        return httpx.Response(200, text="slow")

    async def fake_run_load(cfg: LoadConfig) -> object:
        return await run_load(cfg, transport=httpx.MockTransport(slow_handler))

    monkeypatch.setattr("loadgen.cli.run_load", fake_run_load)
    runner = CliRunner()
    report_path = tmp_path / "underdelivered.json"

    cli_result = runner.invoke(
        app,
        [
            "run",
            "--url",
            "http://service.local/slow",
            "--concurrency",
            "1",
            "--duration",
            "0.2",
            "--rps",
            "100",
            "--json-out",
            str(report_path),
        ],
    )
    assert cli_result.exit_code == 0
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["target_rps"] == 100.0
    assert payload["achieved_rps"] < 90.0
    assert payload["target_rate_sustained"] is False
    combined_output = (cli_result.stderr or "") + (cli_result.output or "")
    assert "coordinated omission" in combined_output.lower()
    assert "not sustained" in combined_output.lower()


@pytest.mark.asyncio
async def test_mixed_200_and_503_error_rate() -> None:
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        if counter["n"] % 2 == 0:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, text="ok")

    transport = httpx.MockTransport(handler)
    config = LoadConfig(
        url="http://service.local/work",
        concurrency=1,
        duration_s=0.15,
        rps=40.0,
    )
    result = await run_load(config, transport=transport)

    assert result.total_requests >= 2
    assert "200" in result.status_codes
    assert "503" in result.status_codes
    expected_rate = result.status_codes["503"] / result.total_requests
    assert result.error_rate == pytest.approx(expected_rate)
    assert result.error_rate > 0.2


def test_cli_exit_code_and_json_report(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        if counter["n"] % 2 == 0:
            return httpx.Response(503, text="overloaded")
        return httpx.Response(200, text="ok")

    async def fake_run_load(cfg: LoadConfig) -> object:
        return await run_load(cfg, transport=httpx.MockTransport(handler))

    monkeypatch.setattr("loadgen.cli.run_load", fake_run_load)
    runner = CliRunner()
    report_path = tmp_path / "evidence.json"

    cli_fail = runner.invoke(
        app,
        [
            "run",
            "--url",
            "http://service.local/work",
            "--concurrency",
            "1",
            "--duration",
            "0.15",
            "--rps",
            "40",
            "--max-error-rate",
            "0.1",
            "--json-out",
            str(report_path),
        ],
    )
    assert cli_fail.exit_code == 1
    assert report_path.exists()
    payload = json.loads(report_path.read_text(encoding="utf-8"))
    assert payload["error_rate"] > 0.1
    assert "503" in payload["status_codes"]

    cli_pass = runner.invoke(
        app,
        [
            "run",
            "--url",
            "http://service.local/work",
            "--concurrency",
            "1",
            "--duration",
            "0.15",
            "--rps",
            "40",
            "--max-error-rate",
            "0.8",
        ],
    )
    assert cli_pass.exit_code == 0


@pytest.mark.asyncio
async def test_transport_errors_categorized_without_crashing() -> None:
    counter = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        counter["n"] += 1
        if counter["n"] % 2 == 1:
            raise httpx.ReadTimeout("timed out waiting for response", request=request)
        raise httpx.ConnectError("connection refused", request=request)

    transport = httpx.MockTransport(handler)
    config = LoadConfig(
        url="http://service.local/unreachable",
        concurrency=1,
        duration_s=0.15,
        rps=40.0,
    )

    result = await run_load(config, transport=transport)

    assert result.total_requests >= 2
    assert result.success_requests == 0
    assert result.error_requests == result.total_requests
    assert result.error_rate == pytest.approx(1.0)
    assert result.status_codes == {}
    assert result.transport_errors.get("timeout", 0) >= 1
    assert result.transport_errors.get("connect_error", 0) >= 1
    assert result.latency_ms == LatencyStats(0.0, 0.0, 0.0, 0.0, 0.0, 0.0)


def test_percentile_math_and_empty_samples() -> None:
    ordered = [10.0, 20.0, 30.0, 40.0, 50.0]
    assert compute_percentile(ordered, 0.0) == pytest.approx(10.0)
    assert compute_percentile(ordered, 50.0) == pytest.approx(30.0)
    assert compute_percentile(ordered, 95.0) == pytest.approx(48.0)
    assert compute_percentile(ordered, 99.0) == pytest.approx(49.6)
    assert compute_percentile(ordered, 100.0) == pytest.approx(50.0)

    assert compute_percentile([42.5], 99.0) == pytest.approx(42.5)
    assert compute_percentile([], 50.0) == 0.0

    empty_stats = LatencyStats.from_samples([])
    assert empty_stats.to_dict() == {
        "min_ms": 0.0,
        "mean_ms": 0.0,
        "p50_ms": 0.0,
        "p95_ms": 0.0,
        "p99_ms": 0.0,
        "max_ms": 0.0,
    }
