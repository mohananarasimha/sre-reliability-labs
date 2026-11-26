# Lab 04: Rolling Update Traffic Blackhole (Pod Termination vs. Endpoint Propagation)

## QUESTION

With readiness probes, `maxUnavailable: 0`, and a Service in front, why does a plain `kubectl rollout restart` still fail requests, and does a `preStop` delay plus graceful drain remove those failures?

## HYPOTHESIS

When a pod is deleted, the kubelet starts termination (preStop, then SIGTERM) while, in parallel, the EndpointSlice controller removes the pod and every node's kube-proxy rewrites its rules. These are independent asynchronous paths with no ordering guarantee.

- **broken:** the app exits on SIGTERM immediately. In-flight requests are cut off (`conn_reset`), and for a short window kube-proxy still routes new connections to an IP that no longer listens (`conn_refused`) or no longer exists (`timeout`).
- **fixed:** a `preStop` sleep keeps the old pod serving until endpoint removal has propagated; SIGTERM then stops the listener and waits for in-flight requests. Expected client-visible failures: 0.

## SETUP

- **Cluster:** `kind` `sre-lab04` (1 control-plane + 2 workers, Kubernetes v1.35.0, iptables kube-proxy), namespace `lab04` with Pod Security Admission `restricted`.
- **App** ([`app/api.py`](app/api.py), stdlib `ThreadingHTTPServer`): `GET /work?ms=300` sleeps 300 ms so requests are in flight; `/readyz` returns 503 once shutdown starts; `/livez` is a process check. `SHUTDOWN_MODE` selects SIGTERM handling:
  - `immediate`: `os._exit(0)` (listener and in-flight requests abandoned).
  - `graceful`: stop accepting, wait up to `DRAIN_TIMEOUT_S` for in-flight requests, then exit. Verified locally before the cluster runs: a 2 s request in flight when SIGTERM arrived completed with 200 in `graceful` mode and failed with `RemoteDisconnected` after 0.77 s in `immediate` mode.
- **Workload:** Deployment `api`, 3 replicas, `RollingUpdate` `maxUnavailable: 0` / `maxSurge: 1`, readiness probe every 2 s, requests 25m/32Mi, limits 200m/96Mi, same image (`sre-lab04-app:local`) for both profiles.
- **Load** ([`app/loadclient.py`](app/loadclient.py)): in-cluster pod hitting the ClusterIP Service `http://api:8080/work?ms=300` (no `port-forward`), open-loop at 20 RPS for 75 s, new TCP connection per request (`Connection: close`), 3 s timeout. Outcomes are counted per send-second: `ok`, `http_5xx`, `conn_refused`, `conn_reset`, `timeout`.
- **Event:** [`run-experiment.sh`](run-experiment.sh) triggers `kubectl rollout restart deployment/api` at t=15 s. It aborts the run if the restart fires more than 3 s off schedule (host sleep / API stall). An observer samples the EndpointSlice once per second (ready and terminating endpoint counts).

```bash
cd labs/04-k8s-drain-traffic-blackhole
make cluster-up build-load      # kind sre-lab04 (1 cp + 2 workers), restricted namespace, image
make run-broken RUN=1           # writes tmp/broken-run1-summary.json
make run-fixed  RUN=1           # writes tmp/fixed-run1-summary.json
make down
```

## VARIABLE

Pod termination handling only:

| Field | broken | fixed |
| --- | --- | --- |
| `SHUTDOWN_MODE` env | `immediate` | `graceful` (`DRAIN_TIMEOUT_S=8`) |
| `lifecycle.preStop` | none | `sleep: {seconds: 10}` (native sleep action) |
| `terminationGracePeriodSeconds` | default (30) | 20 (> 10 s preStop + 8 s drain) |

Replicas, rollout strategy, probes, resources, image, Service, and load are identical. No PodDisruptionBudget is used in either profile: a PDB only gates evictions (`kubectl drain`), not Deployment rollouts, so it would not change this experiment.

## METRIC

- Client outcomes in the baseline window (0–15 s) and the rollout window (15–75 s): total, OK, failures by type, success rate.
- Seconds with any failure.
- Rollout duration (`kubectl rollout status`), EndpointSlice ready / terminating endpoint counts.

## RESULT

Two fresh runs per profile on the same cluster. Baseline (0–15 s) was 300/300 OK in all four runs.

| Profile | Run | Rollout duration | Rollout window OK / total | Success | `timeout` | `conn_reset` | `conn_refused` | `http_5xx` | Seconds with failures |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| broken | 1 | 5 s | 1184 / 1200 | 98.667% | 9 | 5 | 2 | 0 | 16–19 |
| broken | 2 | 4 s | 1182 / 1200 | 98.500% | 16 | 1 | 1 | 0 | 15–19 |
| fixed | 1 | 4 s | 1200 / 1200 | 100.000% | 0 | 0 | 0 | 0 | none |
| fixed | 2 | 4 s | 1200 / 1200 | 100.000% | 0 | 0 | 0 | 0 | none |

EndpointSlice observations (1 s sampling):

- Ready endpoints never dropped below 3 in any run (`maxSurge: 1` + readiness keeps 3 ready pods throughout).
- **broken:** no sample ever caught a `terminating` endpoint. The old pods exited and disappeared from the slice within the 1 s sampling interval.
- **fixed:** old pods stayed in the slice as `terminating` (not ready) for ~10–12 s each (t=17–29 s in run 1), matching the preStop window. Run 2's first samples also show the previous run's pods still terminating from the cleanup at run start; they were not ready and received no new traffic.

## INTERPRETATION

- **The failures are not a capacity problem.** Three ready endpoints existed the whole time, and the rollout itself finished in 4–5 s. All 34 broken-profile failures (across two runs) fall in the 4–5 seconds while old pods were being replaced.
- **Timeouts dominate (25 of 34).** With an immediate exit, the pod sandbox and its IP go away while some kube-proxy rules on the client's node still point at that IP for a moment. The SYN goes nowhere, so the client waits the full 3 s timeout. That is worse for users than a refused connection, because it holds the caller's request for the timeout period instead of failing fast.
- **`conn_reset` is the in-flight cut-off.** Requests already being served (300 ms each, about 2 per pod at 20 RPS) were dropped mid-response when the process called `os._exit`.
- **`conn_refused` is the gap between process exit and network teardown.** The IP was still reachable but nothing was listening.
- **Readiness does not prevent this.** Kubernetes marks a deleting pod's endpoint not-ready on its own; the race is between that propagation and the process stopping, which readiness probes do not order.
- **Why the fixed profile gets zero:** the 10 s preStop sleep holds the old pod fully serving while endpoint removal reaches every node's kube-proxy. By the time SIGTERM arrives, no new connections are routed there, and graceful drain lets the last in-flight requests finish.
- **Magnitude on kind is small (about 1.5% of the rollout window).** kind runs all nodes on one host, so endpoint propagation is fast. On real clusters with many nodes, slower kube-proxy syncs, cloud load balancers, or long-lived keep-alive connections, the same race window is typically longer. This lab shows the mechanism, not a production-sized error rate.
- **Not isolated here:** the fixed profile changes preStop and graceful drain together as one termination-handling change. A graceful-drain-only ablation (no preStop) was not run, so this lab does not show how much each part contributes on its own.

## LESSON

- Zero-downtime rollouts need explicit termination handling. Readiness probes and `maxUnavailable: 0` are not enough.
- Add a `preStop` delay longer than your endpoint/load-balancer propagation time, then drain in-flight work on SIGTERM.
- Size `terminationGracePeriodSeconds` greater than preStop + maximum drain time, or the kubelet's SIGKILL reintroduces the cut-off.
- Watch for client `timeout`s during rollouts, not just 5xx: in this lab, 0 of 34 failures were HTTP errors.

## Prevention

- Platform default in the golden-path chart: `preStop` sleep + documented graceful-shutdown contract for every HTTP service.
- Admission or CI policy: flag Deployments serving traffic that have no `preStop` and an unchanged default grace period.
- Rollout SLO checks: run a synthetic in-cluster client during staging rollouts and fail the pipeline on any non-2xx or timeout.
