#!/bin/bash
# Isolation test: uvicorn only, no celery. If this stays up on Railway, the
# crash is celery-related (OOM or import). Re-enable celery once confirmed.
set -e

: "${PORT:=8000}"
: "${CELERY_CONCURRENCY:=1}"

if [ "${DISABLE_WORKER:-0}" != "1" ]; then
    echo "[start.sh] launching celery worker (pool=${CELERY_POOL:-solo}, concurrency=${CELERY_CONCURRENCY})"
    celery -A celery_app worker \
        --loglevel=INFO \
        --pool="${CELERY_POOL:-solo}" \
        --concurrency="${CELERY_CONCURRENCY}" \
        --without-mingle \
        --without-gossip \
        --without-heartbeat &
else
    echo "[start.sh] worker disabled via DISABLE_WORKER=1"
fi

echo "[start.sh] launching uvicorn on 0.0.0.0:${PORT}"
exec uvicorn main:app --host 0.0.0.0 --port "${PORT}"
