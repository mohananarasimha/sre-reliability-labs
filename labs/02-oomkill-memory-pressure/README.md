# Lab 02 — Cgroup OOM Kill from Unbounded Retention

## QUESTION

Under the same 96 MiB memory limit, does bounding an in-process cache change the outcome from repeated kernel OOM kills to stable service, and what evidence proves an OOM kill actually happened?

## HYPOTHESIS

With an unbounded cache, every `/ingest` request retains 1 MiB, so at 15 RPS the process crosses `memory.max` within seconds, the kernel OOM killer sends `SIGKILL`, the container exits with code 137 and Docker restarts it. With a 24-entry LRU cache (~24 MiB) under the same limit, memory plateaus below the limit and no OOM kill occurs.

## SETUP

- Shared observability stack: `make obs-up` from the repo root.
- Service: `python:3.12-slim`, non-root, read-only rootfs, `cap_drop: [ALL]`, `no-new-privileges`, bound to `127.0.0.1:8082`, gunicorn `--workers 1 --threads 4`.
- Both profiles: `mem_limit: 96m`, `memswap_limit: 96m` (no swap, so exhaustion goes straight to the OOM killer), `restart: "on-failure:5"`, `PAYLOAD_KB=1024`.
- `gunicorn.conf.py` exits the master with 137 when the worker was OOM-killed, so the kill is visible to Docker instead of being hidden by a respawned worker.
- Load: `shared/loadgen`, `concurrency=4`, `rps=15`, `duration=15s`, `timeout=3s`, error budget `--max-error-rate 0.02`.
- Evidence: `make load` records `docker events` (`oom`, `die` with `exitCode`) for the container during the load window, plus `docker inspect` `RestartCount`. `summarize_oom.py` writes `tmp/oom-summary-<profile>.json`.

## VARIABLE

Only `CACHE_MODE` differs: `broken` = `unbounded`, `fixed` = `bounded_lru` (`CACHE_MAX_ENTRIES=24`). The fixed profile also enables the app's soft memory guard (`MEM_SOFT_LIMIT_PCT=75`), which only acts in `bounded_lru` mode. It evicts cache entries and, if that is not enough, returns `503` with `Retry-After: 1`.

## METRIC

Docker `oom` events, `die` events with exit code 137, `RestartCount`, client error rate by type, and post-load cgroup memory.

## RESULT

Measured locally on OrbStack Docker (cgroup v2), two runs per profile, each on a fresh container. Both runs gave the same counts:

| Metric | `broken` (unbounded) | `fixed` (bounded LRU, 24 entries) |
|---|---|---|
| Requests | 225 | 225 |
| `200 OK` | 200 | 222 |
| Transport errors | 25 (run 1: 13 connect + 12 protocol; run 2: 14 + 11) | 0 |
| `503` (soft guard shed) | 0 | 3 |
| Error rate | 11.11% (`make load` fails) | 1.33% (`make load` passes) |
| Docker `oom` events | **3** | 0 |
| Docker `die` with `exitCode=137` | **3** | 0 |
| `RestartCount` after load | 3 | 0 |
| Post-load `memory.current` | — (container mid-restart cycle) | 66.2 MB / 96 MiB (65.8%), 24 cache entries |

## INTERPRETATION

- `broken` hit the limit three times in 15 s. Each `oom` event was paired with a `die exitCode=137` in the same second and followed by a restart. Clients saw `protocol_error` (connection reset mid-response) during the kill and `connect_error` while the container restarted.
- After a restart, `docker inspect` shows `State.OOMKilled=false`, and the cgroup's `memory.events oom_kill` counter starts again from 0 because the restarted container gets a new cgroup. Point-in-time state hides past kills, so the Docker event stream (or Kubernetes `lastState.terminated.reason: OOMKilled`) is the reliable evidence. This is why the lab records events instead of reading `oom_kill` after the run.
- `fixed` never reached the kernel limit under the same 96 MiB. The soft guard shed 3 requests (1.33%) at transient peaks. Evicting cache entries does not immediately lower `memory.current`, because freed memory is not always returned to the kernel right away. A `503` with `Retry-After` is still a better failure than a `SIGKILL` that drops every in-flight request.

## LESSON

Bound every in-memory structure by size, and treat a container memory limit as a hard wall, not a target. To prove an OOM kill, use events or last-termination state, not current state or a counter that resets with the container.

## Run it

```bash
make obs-up                         # repo root
cd labs/02-oomkill-memory-pressure
make up-broken && make load         # exits non-zero: error budget exceeded
make up-fixed  && make load
make inspect-memory
make down
```

```promql
cgroup_memory_current_bytes / cgroup_memory_max_bytes
rate(app_load_shed_total[30s])
rate(http_requests_total{job="lab02-oomkill-memory"}[30s])
```

## Prevention

- Require entry-count and byte caps on caches, buffers and queues in code review.
- Alert on memory headroom (`current / max > 0.8`) and page on any OOM termination reason.
- Use runtime soft limits (`GOMEMLIMIT`, `-XX:MaxRAMPercentage`, explicit cgroup checks) so the service sheds load before the kernel kills it.
- Keep `memswap_limit == mem_limit` for latency-sensitive services so leaks surface quickly instead of turning into swap thrashing.
