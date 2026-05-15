#!/bin/bash
# Multi-process launcher for single-container Railway deploys.
#
# Celery worker runs in the background; uvicorn replaces the shell as PID 2
# under tini. tini (PID 1) reaps the backgrounded celery zombie and forwards
# signals to uvicorn cleanly. When uvicorn exits, tini exits, container exits.
set -e

: "${PORT:=8000}"
: "${CELERY_CONCURRENCY:=2}"

echo "[start.sh] launching celery worker (concurrency=${CELERY_CONCURRENCY})"
celery -A celery_app worker \
    --loglevel=INFO \
    --concurrency="${CELERY_CONCURRENCY}" \
    --without-mingle \
    --without-gossip \
    --without-heartbeat &

echo "[start.sh] launching uvicorn on 0.0.0.0:${PORT}"
exec uvicorn main:app --host 0.0.0.0 --port "${PORT}"
