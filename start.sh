#!/usr/bin/env bash
# start.sh — Launch the trading daemon + web dashboard on Render
#
# The daemon runs as a background process; gunicorn serves the dashboard
# in the foreground (Render monitors gunicorn for health checks).

set -e

# Ensure /data directory exists for persistent storage
if [ -d "/data" ]; then
    export DATA_DIR="/data"
    echo "[start] Using persistent disk at /data"
else
    echo "[start] No persistent disk — using local storage"
fi

# Initialize the database
python -c "from trading.db.store import init_db; init_db()"

# Start the trading daemon in the background
INTERVAL=${DAEMON_INTERVAL_HOURS:-4}
echo "[start] Launching trading daemon (interval=${INTERVAL}h, mode=${TRADING_MODE:-paper})..."
python -m trading daemon --paper --interval "$INTERVAL" &
DAEMON_PID=$!
echo "[start] Daemon PID: $DAEMON_PID"

# Trap signals to shut down cleanly
cleanup() {
    echo "[start] Shutting down daemon (PID $DAEMON_PID)..."
    kill "$DAEMON_PID" 2>/dev/null || true
    wait "$DAEMON_PID" 2>/dev/null || true
    echo "[start] Clean shutdown complete."
    exit 0
}
trap cleanup SIGTERM SIGINT

# Start the web dashboard via gunicorn (foreground)
PORT=${PORT:-10000}
echo "[start] Starting dashboard on port $PORT..."
exec gunicorn \
    --bind "0.0.0.0:$PORT" \
    --workers 2 \
    --threads 4 \
    --timeout 120 \
    --access-logfile - \
    --error-logfile - \
    "trading.monitor.web:app"
