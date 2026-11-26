#!/usr/bin/env bash
set -euo pipefail

PROFILE="${1:-}"
RUN_ID="${2:-1}"

if [[ "${PROFILE}" != "broken" && "${PROFILE}" != "fixed" ]]; then
  echo "Usage: $0 <broken|fixed> [run_id]" >&2
  exit 1
fi

CLUSTER_NAME="sre-lab04"
KCTX="kind-${CLUSTER_NAME}"
NS="lab04"
BASELINE_S=15
DURATION_S=75

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR="${SCRIPT_DIR}/tmp"
mkdir -p "${TMP_DIR}"

CLIENT_LOG="${TMP_DIR}/${PROFILE}-run${RUN_ID}-client.jsonl"
TIMELINE_LOG="${TMP_DIR}/${PROFILE}-run${RUN_ID}-timeline.jsonl"
SUMMARY_JSON="${TMP_DIR}/${PROFILE}-run${RUN_ID}-summary.json"

k() {
  kubectl --context "${KCTX}" -n "${NS}" "$@"
}

echo "[lab04] Preparing clean run: profile=${PROFILE}, run=${RUN_ID} (context=${KCTX})"
k delete pod loadclient --ignore-not-found=true --wait=true --timeout=30s >/dev/null 2>&1 || true
k delete deployment api --ignore-not-found=true --wait=true --timeout=60s >/dev/null 2>&1 || true

# Deploy Service + selected profile Deployment.
k apply -f "${SCRIPT_DIR}/k8s/api-service.yaml" >/dev/null
k apply -f "${SCRIPT_DIR}/k8s/api-${PROFILE}.yaml" >/dev/null

echo "[lab04] Waiting for 3/3 api pods (${PROFILE}) to become Ready..."
k rollout status deployment/api --timeout=90s
k wait --for=condition=Ready pod -l app=api --timeout=60s >/dev/null

# Start in-cluster open-loop load client pod hitting ClusterIP Service (no port-forward).
: > "${TIMELINE_LOG}"
k apply -f "${SCRIPT_DIR}/k8s/loadclient.yaml" >/dev/null
k wait --for=jsonpath='{.status.phase}'=Running pod/loadclient --timeout=45s >/dev/null

START_EPOCH="$(date +%s)"
echo "[lab04] loadclient Running at t=0s (baseline=${BASELINE_S}s, rollout restart at t=${BASELINE_S}s, total=${DURATION_S}s @ 20 RPS)"

# Background observer polling EndpointSlice ready count every 1s.
(
  while true; do
    NOW="$(date +%s)"
    ELAPSED=$((NOW - START_EPOCH))
    if (( ELAPSED > DURATION_S + 2 )); then
      break
    fi
    EPS_JSON="$(k get endpointslices -l kubernetes.io/service-name=api -o json 2>/dev/null || echo '{"items":[]}')"
    python3 -c "
import json, sys
elapsed = int(sys.argv[1])
eps_doc = json.loads(sys.argv[2])
ready_ep = 0
terminating_ep = 0
for sl in eps_doc.get('items', []):
    for ep in sl.get('endpoints', []) or []:
        cond = ep.get('conditions', {}) or {}
        if cond.get('ready') is True:
            ready_ep += 1
        if cond.get('terminating') is True:
            terminating_ep += 1
print(json.dumps({'elapsed_s': elapsed, 'ready_endpoints': ready_ep, 'terminating_endpoints': terminating_ep}))
" "${ELAPSED}" "${EPS_JSON}" >> "${TIMELINE_LOG}"
    sleep 1
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

# Phase boundaries are wall-clock; abort if host sleep or API stall shifts timing.
MAX_SCHEDULE_DRIFT_S="${MAX_SCHEDULE_DRIFT_S:-3}"
assert_on_schedule() {
  local label="$1" planned="$2" actual
  actual=$(( $(date +%s) - START_EPOCH ))
  if (( actual - planned > MAX_SCHEDULE_DRIFT_S )); then
    echo "[lab04] ERROR: ${label} fired at t=${actual}s, planned t=${planned}s (drift > ${MAX_SCHEDULE_DRIFT_S}s). Discarding run." >&2
    exit 1
  fi
}

while (( $(date +%s) - START_EPOCH < BASELINE_S )); do
  sleep 1
done
assert_on_schedule "rollout restart" "${BASELINE_S}"
ROLLOUT_START="$(date +%s)"
echo "[lab04] t=$(( ROLLOUT_START - START_EPOCH ))s: Triggering kubectl rollout restart deployment/api"
k rollout restart deployment/api
assert_on_schedule "rollout restart" "${BASELINE_S}"

echo "[lab04] Waiting for rolling update to complete..."
k rollout status deployment/api --timeout=90s
ROLLOUT_END="$(date +%s)"
ROLLOUT_ELAPSED=$((ROLLOUT_END - ROLLOUT_START))
echo "[lab04] Rolling update completed in ${ROLLOUT_ELAPSED}s (at t=$(( ROLLOUT_END - START_EPOCH ))s)"

echo "[lab04] Waiting for loadclient pod to finish (${DURATION_S}s window)..."
k wait --for=jsonpath='{.status.phase}'=Succeeded pod/loadclient --timeout=120s >/dev/null

cleanup_observer
trap - EXIT

k logs pod/loadclient > "${CLIENT_LOG}"

python3 "${SCRIPT_DIR}/summarize.py" \
  --profile "${PROFILE}" \
  --run-id "${RUN_ID}" \
  --baseline-s "${BASELINE_S}" \
  --duration-s "${DURATION_S}" \
  --rollout-elapsed-s "${ROLLOUT_ELAPSED}" \
  --client-jsonl "${CLIENT_LOG}" \
  --timeline-jsonl "${TIMELINE_LOG}" \
  --out "${SUMMARY_JSON}"

echo "[lab04] Artifacts written under ${TMP_DIR}/ (${PROFILE}-run${RUN_ID}-*)"
