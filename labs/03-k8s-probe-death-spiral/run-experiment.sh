#!/usr/bin/env bash
set -euo pipefail

PROFILE="${1:-}"
RUN_ID="${2:-1}"

if [[ "${PROFILE}" != "broken" && "${PROFILE}" != "fixed" ]]; then
  echo "Usage: $0 <broken|fixed> [run_id]" >&2
  exit 1
fi

CLUSTER_NAME="sre-lab03"
KCTX="kind-${CLUSTER_NAME}"
NS="lab03"
BASELINE_S=20
SPIKE_S=60
RECOVERY_S=70
DURATION_S=$((BASELINE_S + SPIKE_S + RECOVERY_S))
SPIKE_DELAY_MS=2000
HEALTHY_DELAY_MS=10

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR="${SCRIPT_DIR}/tmp"
mkdir -p "${TMP_DIR}"

CLIENT_LOG="${TMP_DIR}/${PROFILE}-run${RUN_ID}-client.jsonl"
TIMELINE_LOG="${TMP_DIR}/${PROFILE}-run${RUN_ID}-timeline.jsonl"
EVENTS_JSON="${TMP_DIR}/${PROFILE}-run${RUN_ID}-events.json"
SUMMARY_JSON="${TMP_DIR}/${PROFILE}-run${RUN_ID}-summary.json"

k() {
  kubectl --context "${KCTX}" -n "${NS}" "$@"
}

set_downstream_latency() {
  local ms="$1"
  k exec deploy/downstream -- python3 -c "
import http.client
conn = http.client.HTTPConnection('127.0.0.1', 8080, timeout=5)
conn.request('POST', '/admin/latency?ms=${ms}')
resp = conn.getresponse()
print(resp.status, resp.read().decode().strip())
conn.close()
"
}

echo "[lab03] Preparing clean run: profile=${PROFILE}, run=${RUN_ID} (context=${KCTX})"
k delete pod loadclient --ignore-not-found=true --wait=true >/dev/null 2>&1 || true
k delete deployment api --ignore-not-found=true --wait=true >/dev/null 2>&1 || true

# Ensure downstream is ready and reset to healthy latency.
k rollout status deployment/downstream --timeout=60s >/dev/null
echo -n "[lab03] Resetting downstream latency to ${HEALTHY_DELAY_MS}ms: "
set_downstream_latency "${HEALTHY_DELAY_MS}"

# Deploy api Service + selected profile Deployment (fresh pods every run).
k apply -f "${SCRIPT_DIR}/k8s/api-service.yaml" >/dev/null
k apply -f "${SCRIPT_DIR}/k8s/api-${PROFILE}.yaml" >/dev/null

echo "[lab03] Waiting for 3/3 api pods (${PROFILE}) to warm up and become Ready..."
k rollout status deployment/api --timeout=120s
k wait --for=condition=Ready pod -l app=api --timeout=60s >/dev/null

# Start in-cluster load client pod (ClusterIP traffic, no port-forward).
: > "${TIMELINE_LOG}"
k apply -f "${SCRIPT_DIR}/k8s/loadclient.yaml" >/dev/null
k wait --for=jsonpath='{.status.phase}'=Running pod/loadclient --timeout=45s >/dev/null

START_EPOCH="$(date +%s)"
echo "[lab03] loadclient Running at t=0s (baseline=${BASELINE_S}s, spike=${SPIKE_S}s @ ${SPIKE_DELAY_MS}ms, recovery=${RECOVERY_S}s)"

# Background observer polling ready EndpointSlice count + per-pod restarts every 2s.
(
  while true; do
    NOW="$(date +%s)"
    ELAPSED=$((NOW - START_EPOCH))
    if (( ELAPSED > DURATION_S + 2 )); then
      break
    fi
    PODS_JSON="$(k get pods -l app=api -o json 2>/dev/null || echo '{"items":[]}')"
    EPS_JSON="$(k get endpointslices -l kubernetes.io/service-name=api -o json 2>/dev/null || echo '{"items":[]}')"
    python3 -c "
import json, sys
elapsed = int(sys.argv[1])
pods_doc = json.loads(sys.argv[2])
eps_doc = json.loads(sys.argv[3])
ready_ep = 0
for sl in eps_doc.get('items', []):
    for ep in sl.get('endpoints', []) or []:
        if ep.get('conditions', {}).get('ready') is True:
            ready_ep += 1
pods = []
for p in pods_doc.get('items', []):
    name = p['metadata']['name']
    statuses = p.get('status', {}).get('containerStatuses', []) or []
    ready = bool(statuses and statuses[0].get('ready'))
    restarts = int(statuses[0].get('restartCount', 0)) if statuses else 0
    waiting = (statuses[0].get('state', {}).get('waiting', {}) or {}).get('reason') if statuses else None
    pods.append({'name': name, 'ready': ready, 'restarts': restarts, 'waiting_reason': waiting})
print(json.dumps({'elapsed_s': elapsed, 'ready_endpoints': ready_ep, 'pods': pods}))
" "${ELAPSED}" "${PODS_JSON}" "${EPS_JSON}" >> "${TIMELINE_LOG}"
    sleep 2
  done
) &
OBSERVER_PID=$!

cleanup_observer() {
  if kill -0 "${OBSERVER_PID}" >/dev/null 2>&1; then
    kill "${OBSERVER_PID}" >/dev/null 2>&1 || true
    wait "${OBSERVER_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup_observer EXIT

# Wait until baseline window ends, then inject downstream latency spike.
while (( $(date +%s) - START_EPOCH < BASELINE_S )); do
  sleep 1
done
echo -n "[lab03] t=$(( $(date +%s) - START_EPOCH ))s: Injecting downstream latency=${SPIKE_DELAY_MS}ms: "
set_downstream_latency "${SPIKE_DELAY_MS}"

# Hold latency spike for SPIKE_S seconds, then restore healthy latency.
SPIKE_END=$((BASELINE_S + SPIKE_S))
while (( $(date +%s) - START_EPOCH < SPIKE_END )); do
  sleep 1
done
echo -n "[lab03] t=$(( $(date +%s) - START_EPOCH ))s: Restoring downstream latency=${HEALTHY_DELAY_MS}ms: "
set_downstream_latency "${HEALTHY_DELAY_MS}"

# Wait for loadclient pod to finish its full 150s run.
echo "[lab03] Waiting for loadclient pod to complete recovery window (${DURATION_S}s total)..."
k wait --for=jsonpath='{.status.phase}'=Succeeded pod/loadclient --timeout=120s >/dev/null

cleanup_observer
trap - EXIT

k logs pod/loadclient > "${CLIENT_LOG}"
k get events -o json > "${EVENTS_JSON}"

python3 "${SCRIPT_DIR}/summarize.py" \
  --profile "${PROFILE}" \
  --run-id "${RUN_ID}" \
  --baseline-s "${BASELINE_S}" \
  --spike-s "${SPIKE_S}" \
  --duration-s "${DURATION_S}" \
  --client-jsonl "${CLIENT_LOG}" \
  --timeline-jsonl "${TIMELINE_LOG}" \
  --events-json "${EVENTS_JSON}" \
  --out "${SUMMARY_JSON}"

echo "[lab03] Artifacts written under ${TMP_DIR}/ (${PROFILE}-run${RUN_ID}-*)"
