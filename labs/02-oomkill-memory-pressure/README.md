# Lab 02 — Linux Cgroup OOMKill & Memory Pressure

## Scenario

A single-worker Python HTTP ingestion service (`gunicorn --workers 1 --threads 4`) allocates a non-sparse 1 MiB payload (`PAYLOAD_KB=1024`) on every `GET /ingest` request and retains it in an in-memory cache. Two profiles are evaluated under identical synthetic traffic (`concurrency=4`, `duration=15s`, `rps=15`, `timeout=3.0s`):

- **`broken`:** `mem_limit: 96m`, `memswap_limit: 96m`, `restart: "on-failure:5"`, `CACHE_MODE=unbounded`, `PAYLOAD_KB=1024`
- **`fixed`:** `mem_limit: 256m`, `memswap_limit: 256m`, `CACHE_MODE=bounded_lru`, `CACHE_MAX_ENTRIES=32`, `PAYLOAD_KB=1024`, `MEM_SOFT_LIMIT_PCT=75`

Setting `memswap_limit` equal to `mem_limit` disables container swap so memory exhaustion deterministically hits the kernel OOM killer instead of silently paging to disk.

## Symptoms

- In-flight HTTP requests abruptly fail with connection resets (`protocol_error` / `connect_error`) as the worker process is terminated mid-request.
- Container RSS (`VmRSS`) and `cgroup_memory_current_bytes` climb linearly with traffic until hitting `memory.max`, followed by `SIGKILL` (`ExitCode=137`), `memory.events` `oom_kill` increments, and container restarts (`RestartCount > 0`).

## Environment

- **Shared observability stack:** `make obs-up` from repository root (`sre-lab-net`, Prometheus `:9090`, Grafana `:3000`).
- **Service container:** `python:3.12-slim`, non-root `appuser` (`uid 10001`), read-only rootfs with `/tmp` `tmpfs`, `cap_drop: [ALL]`, `no-new-privileges:true`, bound to `127.0.0.1:8082`.
- **Telemetry exposed by service:**
  - `/ingest` — allocates a 1 MiB non-zero `bytearray` (forcing Linux to fault physical pages into RSS) and stores it in the cache.
  - `/memory-stats` — live `/sys/fs/cgroup/memory.current`, `memory.max`, `memory.events` (`oom`, `oom_kill`), `/proc/self/status` `VmRSS`, `cache_entries`, and `cache_bytes`.
  - `/metrics` — Prometheus metrics: `http_requests_total`, `http_request_duration_seconds`, `app_cache_entries`, `app_cache_bytes`, `app_process_rss_bytes`, `cgroup_memory_current_bytes`, `cgroup_memory_max_bytes`, `cgroup_memory_oom_kills_total`, `app_cache_evictions_total`, `app_load_shed_total`.

## Hypothesis

With `CACHE_MODE=unbounded` and `mem_limit=96m` (no swap), sustained 15 RPS ingestion allocates ~15 MiB/s of resident memory, exhausting the 96 MiB cgroup limit within seconds. The Linux kernel OOM killer sends `SIGKILL` (`exit 137`), dropping in-flight connections and triggering crash-loop restarts. Bounding cache cardinality with an LRU (`CACHE_MAX_ENTRIES=32`), provisioning headroom (`mem_limit=256m`), and enforcing a cgroup-aware soft memory limit (`MEM_SOFT_LIMIT_PCT=75`) caps RSS cleanly and eliminates OOM kills and transport resets.

## Investigation

Ensure the shared observability stack is running first (`make obs-up` from repo root).

### 1. Run the `broken` Baseline

```bash
cd labs/02-oomkill-memory-pressure
make up-broken
make inspect-memory   # capture pre-load baseline
make load             # 15s @ 15 RPS, concurrency=4 -> tmp/load-broken.json (expect non-zero exit)
make inspect-memory   # inspect oom_kills, RestartCount, OOMKilled, ExitCode
```

Inspect Prometheus queries at `http://localhost:9090`:

```promql
cgroup_memory_current_bytes{profile="broken"} / cgroup_memory_max_bytes{profile="broken"}
app_process_rss_bytes
cgroup_memory_oom_kills_total
rate(http_requests_total{job="lab02-oomkill-memory"}[30s])
```

### 2. Run the `fixed` Profile

```bash
make up-fixed
make inspect-memory   # capture pre-load baseline
make load             # identical 15s @ 15 RPS, concurrency=4 -> tmp/load-fixed.json
make inspect-memory   # verify stable RSS, 0 OOM kills, 0 restarts
```

## Evidence

Measured locally on Docker Engine `28.5.2` (cgroup v2, `/sys/fs/cgroup/memory.*`) under identical `shared/loadgen` traffic (`concurrency=4`, `duration=15s`, `target_rps=15`, `timeout=3.0s`, `PAYLOAD_KB=1024`):

| Metric | `broken` (`96m`, `unbounded`) | `fixed` (`256m`, `bounded_lru`) | Delta |
|---|---|---|---|
| `memory_max_bytes` | `100,663,296` (`96 MiB`) | `268,435,456` (`256 MiB`) | `2.67x` ceiling |
| Total Requests (15s) | `225` | `225` | — |
| Success Requests (`200`) | `201` | `225` | `+24` |
| Transport Errors (`connect`/`protocol`/`timeout`) | `24` (`connect_error: 13`, `protocol_error: 11`) | `0` | `-100%` errors |
| Error Rate (%) | `10.67%` (`loadgen exit=1`) | `0.00%` (`loadgen exit=0`) | `-10.67 pp` |
| Latency p50 / p95 / p99 (ms) | `3.57 / 5.29 / 5.78` (completed reqs only) | `4.36 / 5.31 / 5.74` | Stable |
| Post-Load `cache_entries` (`cache_bytes`) | `22` (`22.0 MiB` — reset after 3rd restart) | `32` (`32.0 MiB` — steady plateau) | Bounded at 32 |
| Post-Load `memory_current_bytes` (`usage %`) | `70,066,176` (`69.60%`, climbing toward next OOM) | `72,720,384` (`27.09%`, flat plateau) | `-42.51 pp` |
| Post-Load `process_rss_bytes` | `54,345,728` (`51.8 MiB`, mid-cycle) | `66,797,568` (`63.7 MiB`, bounded) | Flat vs sawtooth |
| Docker `RestartCount` (pre → post load) | `0 → 3` (3 OOM crash-restart cycles in 15s) | `0 → 0` | `-3` restarts |
| Docker `Status` / `OOMKilled` (post-restart snapshot) | `running` / `false` (reset on container restart — see note) | `running` / `false` | — |

**Interpretation:** Under `broken`, each `/ingest` request appended `1 MiB` of resident memory until crossing the `96 MiB` cgroup limit (~60 requests per cycle after ~35 MiB Python/Gunicorn baseline), triggering three OOM crash-restart cycles (`RestartCount: 0 → 3`) within 15 seconds. Clients experienced `24` hard transport failures (`11` mid-response `protocol_error` TCP resets + `13` `connect_error` failures while the container restarted, `10.67%` error rate). Notice an important operational detail: once Docker restarts a container under `on-failure`, the container enters a fresh cgroup (`oom_kills` resets to `0`) and `docker inspect` resets `State.OOMKilled` to `false` while `Status=running`; in production Kubernetes clusters, check `lastState.terminated.reason: OOMKilled` (`exitCode: 137`) and `kube_pod_container_status_restarts_total` so restarts do not hide prior OOM kills. Under `fixed`, the 32-entry LRU plateaued cleanly at `32.0 MiB` cache / `72.7 MiB` total cgroup usage (`27.09%`), delivering `225/225` `200 OK` responses (`0.00%` error rate, `0` restarts).

## Root Cause

1. **Unbounded retention:** Every `/ingest` request retained a 1 MiB resident `bytearray` with no eviction bound, making memory usage proportional to cumulative request count rather than active working set.
2. **Hard cgroup enforcement without swap:** Once anonymous RSS plus runtime overhead reached `memory.max` (96 MiB) with `memswap_limit == mem_limit`, the kernel had no reclaimable pages and invoked the cgroup OOM killer (`SIGKILL`), abruptly severing TCP connections mid-response and exiting with status `137`.
3. **Supervisor OOM propagation:** In multi-process supervisors like Gunicorn, the kernel kills the high-RSS worker first while PID 1 (master) can survive and hide crash loops from orchestrators unless worker OOM kills are propagated to container exit (`gunicorn.conf.py` checks `memory.events` `oom_kill` in `child_exit` and exits `137`).

## Fix

1. **Bounded LRU cache:** Replaced unbounded retention with an `OrderedDict` capped at `CACHE_MAX_ENTRIES=32` (~32 MiB ceiling), evicting the oldest entry before inserting new payloads.
2. **Cgroup-aware soft-limit guard & load shedding:** Before allocation, the service checks `memory.current / memory.max` against `MEM_SOFT_LIMIT_PCT=75`, proactively evicting cache entries and shedding excess load with HTTP `503` (`Retry-After: 1`) rather than hitting kernel `SIGKILL`.
3. **Explicit memory headroom:** Provisioned `mem_limit: 256m` (`memswap_limit: 256m`) so Python runtime overhead, request buffers, and the 32 MiB bounded cache remain comfortably below the hard cgroup ceiling.

## Prevention

- **Bound every in-memory structure:** Require explicit entry count and byte-size caps on all caches, buffers, and queues in code review.
- **Alert on memory headroom and OOM events:** Monitor `container_memory_working_set_bytes / container_spec_memory_limit_bytes > 0.80` and page on any `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}` or cgroup `oom_kill` increment.
- **Set runtime memory soft limits:** Use language-native soft caps where available (`GOMEMLIMIT` in Go, `-XX:MaxRAMPercentage` in JVM, explicit cgroup checks in Python/Node) so services shed or GC cleanly before kernel `SIGKILL`.
- **Disable swap on latency-sensitive containers:** Keep `memswap_limit == mem_limit` (or Kubernetes QoS Guaranteed) so memory leaks surface rapidly in staging instead of turning into multi-second swap thrashing in production.

## Cleanup

```bash
cd labs/02-oomkill-memory-pressure
make down
```
