#!/bin/sh
set -eu

MULTIPROC_DIR="${PROMETHEUS_MULTIPROC_DIR:-/tmp/prom_multiproc}"
rm -rf "${MULTIPROC_DIR}"
mkdir -p "${MULTIPROC_DIR}"

exec gunicorn \
  --workers "${WORKERS:-4}" \
  --worker-class sync \
  --bind 0.0.0.0:8080 \
  --timeout 30 \
  --keep-alive 5 \
  --config /app/gunicorn.conf.py \
  app:application
