# Lab 01 — CFS CPU Throttling at Low Average Utilization

## QUESTION

Can a container with a CPU limit throttle hard and inflate tail latency even when its **average** CPU utilization is well below that limit?

## HYPOTHESIS

Linux CFS enforces `cpu.max` as a quota per 100 ms period, not as an average. A request that runs 4 threads in parallel burns `4 × 20 ms = 80 ms` of CPU in about 20 ms of wall time. With `cpus=0.5` the quota is 50 ms per period, so the burst exhausts it after ~12.5 ms and every thread is frozen until the next period starts. Average utilization stays low (2 requests/s), but latency per request jumps. With `cpus=1.0` (100 ms quota) the same burst fits inside one period and is rarely throttled.

## SETUP

- Shared observability stack: `make obs-up` from the repo root (Prometheus `127.0.0.1:9090`, Grafana `127.0.0.1:3000`).
- Service: `python:3.12-slim`, non-root, read-only rootfs, `cap_drop: [ALL]`, `no-new-privileges`, bound to `127.0.0.1:8081`, one gunicorn worker (`WORKERS=1`).
- `/work` spawns `PARALLELISM=4` threads. Each thread runs `hashlib.sha256` over a 1 MiB buffer until it has used `WORK_MS=20` ms of its own CPU time (`time.thread_time()`). hashlib releases the GIL for large buffers, so the threads really run in parallel.
- `/cgroup-stats` and `/metrics` expose `cpu.stat` (`usage_usec`, `nr_periods`, `nr_throttled`, `throttled_usec`) and `cpu.max`.
- Load: `shared/loadgen`, `concurrency=1`, `rps=2`, `duration=30s` (60 requests). Open-loop rate was sustained in every run (`target_rate_sustained: true`).
- `make load` snapshots `/cgroup-stats` before and after the run and `cgroup_delta.py` computes:
  - throttled-period ratio = `Δnr_throttled / Δnr_periods`
  - average utilization of the limit = `Δusage_usec / (wall_seconds × quota/period × 1e6)`

## VARIABLE

Only `cpus` differs between profiles: `broken` = `0.5`, `fixed` = `1.0`. Workers, threads, CPU work per request, image and load are identical.

## METRIC

Client latency p50/p95/p99, throttled-period ratio, throttled time, and average CPU utilization of the limit during the load window.

## RESULT

Measured locally on OrbStack Docker (cgroup v2), two runs per profile, each on a fresh container:

| Metric | `broken` run 1 | `broken` run 2 | `fixed` run 1 | `fixed` run 2 |
|---|---|---|---|---|
| `cpu.max` | `50000 100000` | `50000 100000` | `100000 100000` | `100000 100000` |
| Requests / errors | 60 / 0 | 60 / 0 | 60 / 0 | 60 / 0 |
| Latency p50 (ms) | 76.81 | 52.58 | 25.85 | 26.59 |
| Latency p95 (ms) | 178.50 | 156.22 | 36.23 | 51.13 |
| Latency p99 (ms) | 270.35 | 248.76 | 61.40 | 58.49 |
| Throttled periods | 85 / 204 (41.67%) | 41.83% | 6 / 166 (3.61%) | 3.61% |
| Throttled time (s) | 7.99 | 6.62 | 0.39 | 0.30 |
| Avg CPU utilization of limit | **41.80%** | **41.91%** | 20.56% | 20.65% |

Both profiles consumed the same CPU (~6.2 s over 30 s, about 0.21 cores, including metrics scrapes). Only the quota differs.

## INTERPRETATION

- At `cpus=0.5` the container used only ~42% of its limit on average, yet 42% of CFS periods were throttled and p99 was 4–4.4× the `fixed` profile. A dashboard showing "42% of CPU limit" would suggest plenty of headroom.
- Latency in `broken` varies widely (p50 52–77 ms, p99 ~250–270 ms) because the cost of a burst depends on where it lands in the 100 ms period: a burst that starts near a period boundary spreads across two fresh quotas, while one that starts early is frozen for most of the period.
- `fixed` is not completely free of throttling (6 of 166 periods). Its quota is 100 ms against an 80 ms burst plus scrape and runtime overhead, so occasional overlap still crosses the quota. The headroom is small; it is not "unlimited".

## LESSON

Size CPU limits for the **parallel burst per 100 ms period**, not for average utilization. Watch the throttled-period ratio (`nr_throttled / nr_periods`) alongside p99. Average CPU percent cannot show this failure.

## Run it

```bash
make obs-up                         # repo root
cd labs/01-cgroup-cpu-throttling
make up-broken && make load         # tmp/load-broken.json, tmp/cgroup-delta-broken.json
make up-fixed  && make load         # tmp/load-fixed.json,  tmp/cgroup-delta-fixed.json
make down
```

Prometheus queries (also on the Grafana dashboard):

```promql
100 * rate(cgroup_cpu_nr_throttled_total[30s]) / clamp_min(rate(cgroup_cpu_nr_periods_total[30s]), 1e-9)
histogram_quantile(0.99, sum by (le, profile) (rate(http_request_duration_seconds_bucket[30s])))
```

## Prevention

- Alert on throttled-period ratio, not just CPU utilization.
- Match runtime parallelism (thread pools, `GOMAXPROCS`, JVM active processors) to the CPU limit, not the host core count.
- In Kubernetes, latency-sensitive services often set accurate CPU `requests` and either no CPU `limit` or a limit sized for bursts. Choose deliberately; tight limits trade tail latency for noisy-neighbor protection.
