#!/bin/sh
set -eu

exec gunicorn \
  --workers 1 \
  --threads 4 \
  --worker-class gthread \
  --bind 0.0.0.0:8080 \
  --timeout 30 \
  --keep-alive 5 \
  --config /app/gunicorn.conf.py \
  app:application
