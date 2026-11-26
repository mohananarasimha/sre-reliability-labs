#!/usr/bin/env bash
set -euo pipefail

POLICY="${1:-}"
RUN_ID="${2:-1}"

case "${POLICY}" in
  naive|backoff|budgeted) ;;
  *) echo "Usage: $0 <naive|backoff|budgeted> [run_id]" >&2; exit 1 ;;
esac

TRIGGER_AT_S=20
TRIGGER_EXTRA_MS=300
TRIGGER_SECONDS=10
DURATION_S=90
MAX_SCHEDULE_DRIFT_S="${MAX_SCHEDULE_DRIFT_S:-2}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TMP_DIR="${SCRIPT_DIR}/tmp"
mkdir -p "${TMP_DIR}"
PREFIX="${TMP_DIR}/${POLICY}-run${RUN_ID}"
CLIENT_LOG="${PREFIX}-client.jsonl"
STATS_LOG="${PREFIX}-stats.jsonl"
META_JSON="${PREFIX}-meta.json"
SUMMARY_JSON="${PREFIX}-summary.json"

compose() {
  RETRY_POLICY="${POLICY}" docker compose -f "${SCRIPT_DIR}/docker-compose.yml" "$@"
}

echo "[lab05] Fresh stack: policy=${POLICY}, run=${RUN_ID}"
compose --profile client down --remove-orphans >/dev/null 2>&1 || true
compose up -d --wait backend frontend >/dev/null
actual_policy="$(curl -fsS http://127.0.0.1:8085/healthz | python3 -c 'import json,sys; print(json.load(sys.stdin)["retry_policy"])')"
if [[ "${actual_policy}" != "${POLICY}" ]]; then
  echo "[lab05] ERROR: frontend reports policy=${actual_policy}, expected ${POLICY}" >&2
  exit 1
fi

: > "${CLIENT_LOG}"
: > "${STATS_LOG}"
compose --profile client run --rm -T loadclient > "${CLIENT_LOG}" 2>"${PREFIX}-client.stderr" &
CLIENT_PID=$!

(
  while kill -0 "${CLIENT_PID}" >/dev/null 2>&1; do
    epoch="$(python3 -c 'import time; print(f"{time.time():.3f}")')"
    be="$(curl -fsS -m 1 http://127.0.0.1:8086/stats 2>/dev/null || echo null)"
    fe="$(curl -fsS -m 1 http://127.0.0.1:8085/stats 2>/dev/null || echo null)"
    echo "{\"epoch\": ${epoch}, \"backend\": ${be}, \"frontend\": ${fe}}" >> "${STATS_LOG}"
    sleep 1
  done
) &
POLLER_PID=$!

cleanup() {
  kill "${POLLER_PID}" >/dev/null 2>&1 || true
  kill "${CLIENT_PID}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

for _ in $(seq 1 60); do
  [[ -s "${CLIENT_LOG}" ]] && break
  sleep 0.5
done
START_EPOCH="$(head -n1 "${CLIENT_LOG}" | python3 -c 'import json,sys; print(json.load(sys.stdin)["start_epoch"])')"
echo "[lab05] Client started; slowdown of +${TRIGGER_EXTRA_MS}ms for ${TRIGGER_SECONDS}s at t=${TRIGGER_AT_S}s, run=${DURATION_S}s"

python3 -c "import sys,time; time.sleep(max(0.0, float(sys.argv[1]) + float(sys.argv[2]) - time.time()))" \
  "${START_EPOCH}" "${TRIGGER_AT_S}"
TRIGGER_EPOCH="$(python3 -c 'import time; print(f"{time.time():.3f}")')"
curl -fsS -X POST "http://127.0.0.1:8086/admin/slow?ms=${TRIGGER_EXTRA_MS}&seconds=${TRIGGER_SECONDS}" >/dev/null

# Wall-clock phases: abort if host sleep or scheduling delay shifted the trigger.
python3 - "${START_EPOCH}" "${TRIGGER_EPOCH}" "${TRIGGER_AT_S}" "${MAX_SCHEDULE_DRIFT_S}" <<'PY'
import sys
start, trig, planned, limit = map(float, sys.argv[1:])
drift = trig - start - planned
if drift > limit:
    sys.exit(f"[lab05] ERROR: trigger fired {drift:.1f}s late (limit {limit:.0f}s). Discarding run.")
print(f"[lab05] Trigger fired at t={trig - start:.2f}s")
PY

wait "${CLIENT_PID}"
sleep 1.5
cleanup
trap - EXIT

cat > "${META_JSON}" <<EOF
{"policy": "${POLICY}", "run_id": ${RUN_ID}, "start_epoch": ${START_EPOCH}, "trigger_epoch": ${TRIGGER_EPOCH},
 "trigger_at_s": ${TRIGGER_AT_S}, "trigger_extra_ms": ${TRIGGER_EXTRA_MS}, "trigger_seconds": ${TRIGGER_SECONDS},
 "duration_s": ${DURATION_S}}
EOF

python3 "${SCRIPT_DIR}/summarize.py" --meta "${META_JSON}" --client "${CLIENT_LOG}" \
  --stats "${STATS_LOG}" --out "${SUMMARY_JSON}"

compose down >/dev/null
echo "[lab05] Artifacts: ${PREFIX}-*"
