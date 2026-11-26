# Lab 05: Retry Storm and Metastable Overload

## QUESTION

A backend has a 10-second slowdown and then returns to full speed. Does the system recover on its own once the trigger is gone, and how much does that depend on the caller's retry policy?

## HYPOTHESIS

With immediate retries, every request that times out turns into up to 4 backend attempts. If the slowdown fills the backend queue, queue wait alone exceeds the caller's timeout. Callers then give up on every admitted request and retry it, so the backend stays above capacity after the trigger is removed. This is a metastable failure: the system is stable at 100% success before the trigger and stable at near 0% after it, with identical load and identical hardware.

- **naive** (retry immediately): does not recover after the trigger ends.
- **backoff** (exponential backoff with full jitter): spreads retries out in time but does not reduce how many there are. Expected: also does not recover.
- **budgeted** (backoff plus a retry budget capped at ~10% of requests): offered load stays below capacity, so the queue drains. Expected: recovers within seconds of the trigger ending.

## SETUP

All three components are stdlib Python in one image (`python:3.12-slim`, non-root, read-only root filesystem, all capabilities dropped, 1 CPU / 256 MiB per container).

- **Backend** ([`app/backend.py`](app/backend.py)): 4 workers × 20 ms service time = 200 req/s nominal capacity (measured: ~191–193 completions/s when saturated). Requests that find all workers busy wait in a queue of at most 500; above that the backend answers 503 immediately. Admitted requests are always processed, even if the caller has already gone. `POST /admin/slow?ms=300&seconds=10` adds 300 ms to every request that starts service in the next 10 s, then reverts on its own.
- **Frontend** ([`app/frontend.py`](app/frontend.py)): for each user request, calls the backend with a **200 ms timeout per attempt** and **up to 3 retries** on timeout or 503. `RETRY_POLICY` is the only setting that differs between runs.
- **Client** ([`app/loadclient.py`](app/loadclient.py)): open-loop, 120 req/s (60% of nominal capacity) for 90 s, 2 s timeout, new connection per request. Results are bucketed by scheduled send second. `late_sends` counts requests that left more than 50 ms behind schedule (client saturation).
- **Runner** ([`run-experiment.sh`](run-experiment.sh)): recreates the stack, checks that the frontend reports the expected policy, starts the client, and fires the slowdown at t=20 s (backend is slow t=20–30 s). It aborts the run if the trigger fires more than 2 s late. Backend and frontend `/stats` are sampled once per second. [`summarize.py`](summarize.py) computes per-phase success, recovery time and retry amplification.

```bash
cd labs/05-retry-storm-metastability
make run-naive RUN=1       # builds the image, writes tmp/naive-run1-summary.json
make run-backoff RUN=1
make run-budgeted RUN=1
make down
```

Committed summaries are in [`results/`](results/).

## VARIABLE

`RETRY_POLICY` on the frontend: `naive` | `backoff` | `budgeted`.

| Policy | Delay before retry *n* | Retry allowed when |
|---|---|---|
| `naive` | none | attempts < 4 |
| `backoff` | uniform(0, min(1 s, 50 ms × 2^(n−1))) | attempts < 4 |
| `budgeted` | same as `backoff` | attempts < 4 **and** a budget token is available (each request deposits 0.1 token, each retry spends 1, bucket max 10) |

Attempt timeout, maximum retries, load, trigger, backend capacity and queue size are identical in every run.

## METRIC

- Client success rate per phase: baseline (t=0–20 s), after the trigger ended (t=30–90 s), and the last 30 s (t=60–90 s).
- Recovery time: seconds after the trigger ended until the first 5 consecutive seconds with ≥99% success.
- Retry amplification: backend attempts per frontend request, t=30–90 s.
- Backend waiting-queue depth.

## RESULT

Two trials per policy. The trigger fired at t=20.02–20.03 s in every run.

| Policy | Trial | Baseline success | Success t=30–90 s | Success t=60–90 s | Recovered after trigger end | Backend attempts per request (t=30–90 s) | Retries denied by budget (t=30–90 s) |
|---|---|---|---|---|---|---|---|
| naive | 1 | 100% | 2.17% (156/7200) | 2.00% | not within 60 s | 3.95 | – |
| naive | 2 | 100% | 2.18% (157/7200) | 2.64% | not within 60 s | 3.95 | – |
| backoff | 1 | 100% | 1.90% (137/7200) | 1.97% | not within 60 s | 3.97 | – |
| backoff | 2 | 100% | 1.90% (137/7200) | 1.97% | not within 60 s | 3.97 | – |
| budgeted | 1 | 100% | 86.57% (6233/7200) | 100% | 9 s | 1.01 | 1032 |
| budgeted | 2 | 100% | 86.64% (6238/7200) | 100% | 9 s | 1.02 | 1026 |

(The two `backoff` trials ended with the same totals; their per-second counts differ.)

Success rate in 5-second buckets, trial 1:

| t (s) | naive | backoff | budgeted |
|---|---|---|---|
| 0–19 | 100% | 100% | 100% |
| 20–24 (slow) | 0.67% | 0.83% | 0.67% |
| 25–29 (slow) | 0.0% | 0.0% | 0.0% |
| 30–34 | 1.5% | 2.17% | 1.0% |
| 35–39 | 2.0% | 2.0% | 37.83% |
| 40–89 | 1.17–3.83% | 0.67–2.83% | 100% |

In the naive and backoff runs, the backend queue held 493–500 waiting requests in every sample from t=24 s to t=89 s. In both budgeted runs it was 500 at t=29.5 s, then fell steadily (483, 425, 362, 299, 237, 176, 114, 50 in trial 1; about 61 per second). It reached 0 at t=38.9 s and stayed at 0–3 for the rest of the run.

Backend throughput during the stuck phase of naive trial 1 was 190.6–193.0 completions/s (samples at t=31–91 s). The backend was running at full capacity while users saw about 2% success.

Client saturation: `late_sends` was 0 in every run except naive trial 2, where 28 of 10,800 requests were sent more than 50 ms late, all during the stuck phase.

## INTERPRETATION

**Metastability.** After t=30 s the trigger is gone and the backend completes ~192 requests/s, so it is not slow. But its queue is full: 500 waiting requests at ~192/s means about 2.6 s of queue wait, far above the 200 ms attempt timeout. Every admitted request finishes after its caller has already left, so almost all of that work is wasted. The frontend, seeing timeouts and 503s, sends ~3.95 attempts per user request: about 475 attempts/s against ~192/s of capacity. That keeps the queue full, which keeps the timeouts going. The 10-second trigger started the loop; retries alone sustain it.

**Why backoff did not help.** Backoff and jitter change *when* a retry is sent, not *whether* it is sent. Attempts per request stayed at 3.97. With the delays in this setup (at most 50 + 100 + 200 ms), all retries still arrive within the same second as the original request, so the offered load is the same. Jitter addresses clients retrying in lockstep. It does not address the volume of retries, and volume is what keeps this system stuck.

**Why the budget did.** The budget caps retries at about 10% of requests. During the slowdown it denied ~1,030 retries, and those requests failed fast instead of joining the queue. After the slowdown, offered load is at most ~1.1 × 120 = 132 attempts/s, below the ~192/s capacity. The expected drain rate is about 192 − 132 = 60/s; the measured rate was about 61/s, emptying 500 waiting requests in about 9 s, which matches the 9 s recovery. During the slowdown itself all three policies were equally bad (0–1% success), so the budget gave up nothing.

## LESSON

Recovery after a fault depends on whether retry volume fits in the spare capacity, not on retry timing. A retry budget makes that bound explicit: if (1 + budget ratio) × normal load is below capacity, the system cannot hold itself in overload once the trigger is removed. Backoff with jitter is still worth having, but it is not overload protection.

## LIMITATIONS

- One frontend instance. The budget is per process. With many frontends, each still caps its own retry ratio, but that fleet-wide behaviour was not tested here.
- Two trials per policy, one load level (60%), one trigger size (+300 ms for 10 s). The load and trigger size at which naive retries do recover were not measured.
- "Not within 60 s" is what was observed, not "never". Stopping the load would end it; the queue was already draining by the final sample after the client stopped.
- Other ways to break the loop were not tested: dropping queued requests whose caller has already timed out (deadline propagation or a queue timeout), LIFO queueing, or admission control on the backend.
- Service time is a `time.sleep` in Python, not CPU work; measured saturated throughput was ~192/s against the 200/s nominal.
