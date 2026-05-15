#!/bin/sh
# Multi-process launcher for single-container Railway deploys.
#
# Starts the Celery worker in the background and uvicorn in the foreground.
# If either process exits, we exit too so the orchestrator can restart the
# whole container — no half-alive states where the API is up but the worker
# is dead (which would leave jobs stuck forever in 'pending').
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
WORKER_PID=$!

echo "[start.sh] launching uvicorn on 0.0.0.0:${PORT}"
uvicorn main:app --host 0.0.0.0 --port "${PORT}" &
WEB_PID=$!

# Trap signals — forward to children so graceful shutdown works.
shutdown() {
    echo "[start.sh] received shutdown signal, stopping children"
    kill -TERM "$WORKER_PID" "$WEB_PID" 2>/dev/null || true
    wait "$WORKER_PID" "$WEB_PID" 2>/dev/null || true
    exit 0
}
trap shutdown TERM INT

# Wait for either child to exit. If one dies, exit so the container restarts.
wait -n "$WORKER_PID" "$WEB_PID"
EXIT_CODE=$?
echo "[start.sh] a child process exited with code ${EXIT_CODE}, shutting down"
kill -TERM "$WORKER_PID" "$WEB_PID" 2>/dev/null || true
wait "$WORKER_PID" "$WEB_PID" 2>/dev/null || true
exit "$EXIT_CODE"
