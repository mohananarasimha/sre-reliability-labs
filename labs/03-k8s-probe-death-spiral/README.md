# Lab 03 — Kubernetes Liveness Probe Death Spiral

## QUESTION

Does a Kubernetes `livenessProbe` that depends on a slow downstream service turn a temporary dependency latency spike into a synchronized restart cascade whose outage outlives the spike?

## HYPOTHESIS

When `livenessProbe` calls a deep endpoint (`/healthz/deep`) that queries a shared downstream dependency with a tight timeout (`timeoutSeconds: 1`, `periodSeconds: 5`, `failureThreshold: 3`), a 60-second 2-second latency spike on the downstream fails liveness across all 3 `api` pods simultaneously. Kubelet kills and restarts every pod, dropping ready `EndpointSlice` endpoints to `0/3`. Because each restarted container requires 12 seconds of startup warmup and has no `startupProbe`, repeated kills leave all pods unready even after the downstream recovers — extending client failures past the end of the spike. Moving liveness to a process-only check (`/livez`), adding a `startupProbe` covering warmup, and keeping readiness local (`/readyz`) will prevent all restarts, preserve `3/3` ready endpoints, and allow immediate recovery once downstream latency returns to normal.

## SETUP

- **Cluster:** Local single-node `kind` cluster (`sre-lab03`, Kubernetes v1.35.0 on OrbStack Docker), namespace `lab03` enforcing Pod Security Admission `restricted`.
- **Image:** Local `sre-lab03-app:local` (`python:3.12-slim`, stdlib HTTP servers, non-root UID `10001`, `readOnlyRootFilesystem: true`, `allowPrivilegeEscalation: false`, `capabilities.drop: ["ALL"]`, `imagePullPolicy: IfNotPresent`).
- **Services:**
  - `downstream` (1 replica, `ClusterIP:8080`): `/query` sleeps for a runtime-configurable delay (`POST /admin/latency?ms=<N>`), initialized at `10 ms`.
  - `api` (3 replicas, `ClusterIP:8080`, `WARMUP_SECONDS=12`, `requests: 25m/32Mi`, `limits: 200m/96Mi`):
    - `/work`: calls `downstream/query` with `1.5s` timeout (`504` on timeout).
    - `/healthz/deep`: returns `503` while warming up, then calls `downstream/query` with `0.9s` timeout (`503` on failure).
    - `/livez`: returns `200` immediately (process check only).
    - `/readyz`: returns `503` while warming up, then `200` (local readiness only).
- **Load:** In-cluster `loadclient` Pod hitting `http://api:8080/work` (ClusterIP, no `port-forward`) at `4 RPS` (`MAX_WORKERS=16`, `TIMEOUT_S=2.0`) for `150s`:
  - `0–20s`: baseline (`downstream` = `10 ms`, 80 requests)
  - `20–80s`: latency spike (`downstream` = `2000 ms`, 240 requests)
  - `80–150s`: recovery (`downstream` = `10 ms`, 280 requests)
- **Observer:** Polls `EndpointSlice` ready endpoint counts and per-pod restart counts every 2 seconds; captures pod events (`Unhealthy`, `Killing`, `BackOff`) filtered to the run's pods.

## VARIABLE

Exactly one variable differs between `k8s/api-broken.yaml` and `k8s/api-fixed.yaml`: **the probe configuration**.

- **`broken`:** `livenessProbe` -> `/healthz/deep` (`initialDelaySeconds: 5`, `periodSeconds: 5`, `timeoutSeconds: 1`, `failureThreshold: 3`); `readinessProbe` -> `/readyz`; no `startupProbe`.
- **`fixed`:** `startupProbe` -> `/readyz` (`periodSeconds: 2`, `timeoutSeconds: 1`, `failureThreshold: 15`); `livenessProbe` -> `/livez` (`periodSeconds: 5`, `timeoutSeconds: 1`, `failureThreshold: 3`); `readinessProbe` -> `/readyz`.

Replicas (3), image, warmup (12s), resources, security context, downstream spike, and client load are identical.

## METRIC

- Total container restarts across the 3 `api` pods
- Minimum ready endpoints in `EndpointSlice` (baseline / spike / recovery)
- Kubernetes pod events (`Unhealthy`, `Killing`, `BackOff`)
- Client request outcome and success rate by phase (`200`, `5xx`, `conn_err`, `timeout`)
- Time to full recovery after `t=80s` (both `client_100pct_ok_s` for 3 consecutive 100% `200` seconds and `endpoints_3_of_3_s` sustained)

## RESULT

Measured locally on `kind` (`sre-lab03`, Kubernetes v1.35.0), two independent fresh-deployment runs per profile:

| Metric | `broken` run 1 | `broken` run 2 | `fixed` run 1 | `fixed` run 2 |
|---|---|---|---|---|
| Total pod restarts (3 pods) | **9** (3/3/3) | **9** (3/3/3) | **0** (0/0/0) | **0** (0/0/0) |
| Ready endpoints min (baseline / spike / recovery) | 3 / **0** / **0** | 3 / **0** / **0** | 3 / **3** / **3** | 3 / **3** / **3** |
| Events: `Killing` / `Unhealthy` / `BackOff` | **9** / 75 / 0 | **9** / 75 / 0 | **0** / 18\* / 0 | **0** / 18\* / 0 |
| Baseline (`0–20s`) success rate (`ok/total`) | 100.0% (80/80) | 100.0% (80/80) | 100.0% (80/80) | 100.0% (80/80) |
| Spike (`20–80s`) success rate (`ok/total`) | 1.25% (3/240) | 1.25% (3/240) | 1.67% (4/240) | 0.83% (2/240) |
| Spike failures (`5xx` / `conn_err` / `timeout`) | 120 / **68** / **49** | 112 / **69** / **56** | 236 / **0** / **0** | 238 / **0** / **0** |
| Recovery (`80–150s`) success rate (`ok/total`) | **87.86%** (246/280) | **87.86%** (246/280) | **98.57%** (276/280) | **98.93%** (277/280) |
| Recovery failures (`5xx` / `conn_err` / `timeout`) | 0 / **9** / **25** | 0 / **9** / **25** | 4 / **0** / **0** | 3 / **0** / **0** |
| Client recovery after spike end (`100% ok`) | **9 s** | **9 s** | **1 s** | **1 s** |
| Ready endpoints `3/3` sustained after spike end | **10 s** | **12 s** | **1 s** | **0 s** |
| Full recovery time after spike end | **10 s** | **12 s** | **1 s** | **1 s** |

\* In `fixed`, all 18 `Unhealthy` events (6 per pod) were expected `Startup probe failed: HTTP 503` checks during the initial 12-second warmup prior to `t=0s`. Zero `Unhealthy` or `Killing` events occurred during or after the spike.

## INTERPRETATION

- **Synchronized restart cascade in `broken`:** Because all 3 pods shared the same downstream and probe cadence, kubelet killed all 3 pods in lockstep at `t≈37s`, `t≈58s`, and `t≈77s` (9 total restarts). After each kill, ready endpoints collapsed to `0/3` for the 12-second warmup window.
- **Outage outliving the dependency spike:** When downstream returned to `10 ms` at `t=80s`, `broken` pods had just been killed at `t≈77s` and remained at `0/3` ready endpoints until `t=90–92s`. Clients suffered 34 additional transport failures (`9 conn_err`, `25 timeout`) after the downstream was already healthy, stretching full recovery to `10–12 s` vs `1 s` in `fixed`.
- **Why `BackOff` was 0 here:** `api.py` catches `SIGTERM` and exits cleanly with status `0` while surviving ~15s between probe kills, so kubelet restarted containers immediately rather than entering `CrashLoopBackOff`. Services that exit non-zero on `SIGKILL` or fail faster during warmup also trigger exponential `CrashLoopBackOff` (`10s -> 20s -> 40s`), extending the post-spike outage well beyond warmup time.
- **Honest behavior in `fixed` during the spike:** Fixing probes does **not** make `/work` succeed while `downstream` takes `2000 ms` — `/work` still returned `504 Gateway Timeout` on `236–238 / 240` spike requests (and `3–4` already in-flight requests at `t=80s`). What changed is failure containment: **zero restarts**, **zero connection resets/client timeouts**, **`3/3` ready endpoints throughout**, and **1-second recovery** once downstream healed.
- **Readiness design tradeoff (local `/readyz` vs dependency-coupled `/readyz`):** This lab deliberately keeps `readinessProbe` local (checking warmup completion only). Coupling `readinessProbe` to a shared downstream is its own failure mode: when the downstream degrades, every pod fails readiness simultaneously, emptying the Service `EndpointSlice` (`0/3`) and causing a total cluster-level blackhole (dropping non-dependent routes, cached responses, and graceful `504` error handling) while delaying recovery until readiness probes pass again.

## LESSON

- **Never put external dependency checks inside `livenessProbe`.** Restarting a healthy process cannot fix a slow database or downstream API; it only destroys working replicas, empties `EndpointSlice`s, and turns a transient dependency slowdown into a self-inflicted availability outage.
- **Use `startupProbe` for slow initialization** so liveness never races with warmup or cache loading.
- **Keep `readinessProbe` scoped to whether this specific pod can accept traffic** (warmup complete, local event loop/thread pool healthy), not whether a shared downstream is healthy. Handle downstream slowness in application code with bounded timeouts, circuit breakers, and explicit `503`/`504` responses.

## Run it

```bash
cd labs/03-k8s-probe-death-spiral
make cluster-up               # creates kind cluster sre-lab03 + PSA restricted namespace
make build-load               # builds sre-lab03-app:local, loads into kind, deploys downstream
make run-broken RUN=1         # writes tmp/broken-run1-summary.json
make run-fixed  RUN=1         # writes tmp/fixed-run1-summary.json
make down                     # deletes kind cluster sre-lab03
```

Phase boundaries are wall-clock. `run-experiment.sh` aborts if the spike is injected or restored more than `MAX_SCHEDULE_DRIFT_S` (default 3s) late, because the client's per-phase counts would then be misattributed. On a laptop, keep the host awake during runs (macOS: `caffeinate -i make run-broken RUN=1`).

## Prevention

- Enforce probe policy in CI/admission reviews: `livenessProbe` must target a lightweight local endpoint (`/livez`), never an upstream/DB check.
- Require `startupProbe` for any workload whose startup or cache-warming time can approach `livenessProbe.initialDelaySeconds + periodSeconds * failureThreshold`.
- Alert on pod restart rates (`increase(kube_pod_container_status_restarts_total[5m]) > 0`) and `EndpointSlice` ready-ratio drops alongside downstream latency so probe-induced restarts are caught immediately.
