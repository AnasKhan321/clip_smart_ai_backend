#!/bin/bash
set -e

: "${PORT:=8000}"
: "${CELERY_CONCURRENCY:=1}"

pids=()

cleanup() {
    echo "[start.sh] shutting down children"
    for pid in "${pids[@]}"; do
        kill -TERM "$pid" 2>/dev/null || true
    done
    wait
}
trap cleanup SIGTERM SIGINT

if [ "${DISABLE_WORKER:-1}" != "1" ]; then
    echo "[start.sh] launching celery worker (pool=${CELERY_POOL:-solo}, concurrency=${CELERY_CONCURRENCY})"
    celery -A celery_app worker \
        --loglevel=INFO \
        --pool="${CELERY_POOL:-solo}" \
        --concurrency="${CELERY_CONCURRENCY}" \
        --without-mingle \
        --without-gossip \
        --without-heartbeat &
    pids+=($!)
else
    echo "[start.sh] worker disabled (jobs run inline via threads)"
fi

echo "[start.sh] launching uvicorn on 0.0.0.0:${PORT}"
uvicorn main:app --host 0.0.0.0 --port "${PORT}" &
pids+=($!)

# Exit when any child dies so Railway restarts cleanly.
wait -n
exit_code=$?
echo "[start.sh] child exited with code ${exit_code}, terminating"
cleanup
exit "$exit_code"
