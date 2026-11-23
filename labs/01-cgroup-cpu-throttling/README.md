# Lab 01 — Linux CFS CPU Quota Throttling

## Scenario

A multi-worker Python HTTP service (`gunicorn`) processes deterministic CPU-bound requests (~15 ms of thread CPU time per request) inside a container constrained by a Linux Completely Fair Scheduler (CFS) CPU quota. Two profiles are tested under identical synthetic traffic (`concurrency=8`, `duration=20s`, `rps=25`):

- **`broken`:** `cpus: "0.25"`, `WORKERS=4`, `WORK_MS=15`
- **`fixed`:** `cpus: "2.00"`, `WORKERS=2`, `WORK_MS=15`

## Symptoms

- Severe tail-latency degradation (`p95`/`p99` spikes into hundreds of milliseconds) and reduced achieved throughput on a single endpoint that only requires ~15 ms of CPU per request.
- Wall-clock response time (`wall_ms`) diverges sharply from actual thread CPU execution time (`cpu_ms`).

## Environment

- **Shared observability stack:** `make obs-up` from repository root (attaches `sre-lab-net`, Prometheus `:9090`, Grafana `:3000`).
- **Service container:** `python:3.12-slim`, non-root `appuser`, read-only rootfs with `/tmp` `tmpfs`, `cap_drop: [ALL]`, `no-new-privileges:true`, bound to `127.0.0.1:8081`.
- **Telemetry exposed by service:**
  - `/work` — deterministic CPU-bound endpoint calibrated via `time.thread_time()` (`CLOCK_THREAD_CPUTIME_ID`).
  - `/cgroup-stats` — live `/sys/fs/cgroup/cpu.stat` (cgroup v2, with v1 fallback) and `cpu.max`.
  - `/metrics` — multiprocess Prometheus metrics: `http_requests_total`, `http_request_duration_seconds`, `cgroup_cpu_nr_periods_total`, `cgroup_cpu_nr_throttled_total`, `cgroup_cpu_throttled_seconds_total`.

## Hypothesis

With `cpus: "0.25"` (25 ms CFS quota per 100 ms period) and `WORKERS=4`, four concurrent OS worker processes burn through the entire 25 ms cgroup quota in ~6.25 ms of wall time. The Linux kernel CFS bandwidth controller then throttles all workers for the remaining ~93.75 ms of the period, causing multi-period stalls and severe p99 tail latency. Right-sizing the CPU quota and aligning worker concurrency (`WORKERS=2` with `cpus: "2.00"`) prevents workers from exhausting the CFS period quota and eliminates throttling stalls.

## Investigation

Ensure the shared observability stack is running first (`make obs-up` from repo root).

### 1. Run the `broken` Baseline

```bash
cd labs/01-cgroup-cpu-throttling
make up-broken
make inspect-cgroup   # capture pre-load baseline
make load             # 20s @ 25 RPS, concurrency=8 -> tmp/load-broken.json
make inspect-cgroup   # capture post-load nr_throttled & throttled_usec
```

Inspect Prometheus queries at `http://localhost:9090`:

```promql
rate(cgroup_cpu_nr_throttled_total[30s]) / clamp_min(rate(cgroup_cpu_nr_periods_total[30s]), 1e-9)
rate(cgroup_cpu_throttled_seconds_total[30s])
histogram_quantile(0.99, sum by (le, profile) (rate(http_request_duration_seconds_bucket[30s])))
```

### 2. Run the `fixed` Profile

```bash
make up-fixed
make inspect-cgroup   # capture pre-load baseline
make load             # identical 20s @ 25 RPS, concurrency=8 -> tmp/load-fixed.json
make inspect-cgroup   # capture post-load nr_throttled & throttled_usec
```

## Evidence

Measured locally on Docker Engine `28.5.2` (cgroup v2, `/sys/fs/cgroup/cpu.stat`) under identical `shared/loadgen` traffic (`concurrency=8`, `duration=20s`, `target_rps=25`, `WORK_MS=15`):

| Metric | `broken` (`cpus=0.25`, `WORKERS=4`) | `fixed` (`cpus=2.00`, `WORKERS=2`) | Delta |
|---|---|---|---|
| `cpu.max` | `25000 100000` | `200000 100000` | `8x` quota |
| Total Requests (20s) | `296` | `500` | `+68.9%` |
| Achieved RPS (target 25) | `14.47` | `25.03` | `+73.0%` (sustained target) |
| Error Rate (%) | `0.00%` | `0.00%` | `0.00%` |
| Latency p50 (ms) | `595.43` | `16.13` | `-97.3%` (`36.9x` faster) |
| Latency p95 (ms) | `696.44` | `16.88` | `-97.6%` (`41.3x` faster) |
| Latency p99 (ms) | `702.82` | `18.48` | `-97.4%` (`38.0x` faster) |
| Latency max (ms) | `708.02` | `32.40` | `-95.4%` |
| `nr_periods` (pre → post load) | `35 → 242` (`+207`) | `17 → 219` (`+202`) | — |
| `nr_throttled` (pre → post load) | `22 → 226` (`+204`) | `0 → 0` (`+0`) | `-100%` throttling |
| Throttled Periods Ratio (load window / total) | `98.55%` / `93.39%` | `0.00%` / `0.00%` | `-98.55 pp` |
| `throttled_seconds` (pre → post load) | `1.61s → 86.68s` (`+85.07s`) | `0.00s → 0.00s` (`+0.00s`) | `-85.07s` |

**Interpretation:** Even though each request only performed `15 ms` of CPU work and produced `0%` HTTP errors, `broken` throttled `204` of `207` CFS periods (`98.55%`) during the 20-second load window, accumulating `85.07` thread-seconds of kernel stalls. That pushed p99 latency to `702.82 ms` (`46.8x` the actual CPU work) and capped throughput at `14.47 RPS`. Right-sizing the CPU quota and aligning worker count (`WORKERS=2`, `cpus=2.00`) eliminated CFS throttling completely (`0` throttled periods), restoring full `25.03 RPS` throughput and `18.48 ms` p99 latency.

## Root Cause

Linux CFS enforces container `cpus` limits using a bandwidth quota over a 100 ms window (`cpu.max`: `<quota_usec> 100000`). The quota is shared across all runnable threads/processes in the cgroup:

- At `cpus: "0.25"`, the cgroup receives `25,000 µs` (25 ms) of CPU time per `100,000 µs` (100 ms) wall-clock period.
- Running `WORKERS=4` allows up to 4 CPU cores to be consumed in parallel, exhausting 25 ms of quota in `25 / 4 = 6.25 ms` of wall time.
- Once exhausted, the kernel freezes every runnable worker in the cgroup until the next 100 ms period begins, injecting ~75–94 ms stall penalties per period into in-flight requests and starving throughput.

## Fix

1. Size the container CPU quota above peak sustained CPU demand plus burst headroom (`cpus: "2.00"` -> `200,000 µs` per `100,000 µs` period).
2. Align worker process/thread concurrency to the allocated CPU budget (`WORKERS=2 <= 2.00` CPUs), ensuring concurrent workers cannot exceed the per-period CFS quota even during traffic bursts.

## Prevention

- **Monitor CFS throttling directly:** Alert on throttled period ratio (`rate(container_cpu_cfs_throttled_periods_total[5m]) / rate(container_cpu_cfs_periods_total[5m]) > 0.25`) alongside p99 latency, never relying on average CPU utilization alone.
- **Align runtime concurrency with container CPU limits:** Set web worker counts, Go `GOMAXPROCS` (`automaxprocs`), or JVM active processor counts to match container CPU requests/limits rather than host core count.
- **Evaluate Kubernetes CPU limits deliberately:** In latency-sensitive Kubernetes workloads, set accurate CPU `requests` for scheduling/shares and avoid overly tight CPU `limits` unless strict multi-tenant noisy-neighbor capping is required.

## Cleanup

```bash
cd labs/01-cgroup-cpu-throttling
make down
```
