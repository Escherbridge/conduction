#!/usr/bin/env bash
# Launch Conduction
#
# Defaults: host 127.0.0.1, port 8000, 30 s startup timeout, browser opened.
# Every default can be overridden by a flag or the matching CONDUCTION_* env var.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

PORT="${CONDUCTION_PORT:-8000}"
HOST="${CONDUCTION_HOST:-127.0.0.1}"
MAX_WAIT="${CONDUCTION_START_TIMEOUT:-30}"
NO_BROWSER="${CONDUCTION_NO_BROWSER:-0}"
DO_STOP=0

usage() {
    cat <<'USAGE'
Usage: conduction.sh [options]

  --port N          TCP port to serve on          (env CONDUCTION_PORT, default 8000)
  --host ADDR       interface to bind             (env CONDUCTION_HOST, default 127.0.0.1)
  --timeout N       seconds to wait for readiness (env CONDUCTION_START_TIMEOUT, default 30)
  --no-browser      do not open a browser         (env CONDUCTION_NO_BROWSER=1)
  --stop            stop a server started by this script, then exit
  -h, --help        show this help
USAGE
}

while [ $# -gt 0 ]; do
    case "$1" in
        --port)       PORT="$2"; shift 2 ;;
        --host)       HOST="$2"; shift 2 ;;
        --timeout)    MAX_WAIT="$2"; shift 2 ;;
        --no-browser) NO_BROWSER=1; shift ;;
        --stop)       DO_STOP=1; shift ;;
        -h|--help)    usage; exit 0 ;;
        *)            echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    esac
done

# 0.0.0.0 is a bind address, not a reachable one; probe and browse over loopback.
case "$HOST" in
    0.0.0.0|::) PROBE_HOST="127.0.0.1" ;;
    *)          PROBE_HOST="$HOST" ;;
esac
BASE_URL="http://$PROBE_HOST:$PORT"
PING_URL="$BASE_URL/api/ping"

LOG_PATH="$SCRIPT_DIR/.agentgraph/app.log"
PID_PATH="$SCRIPT_DIR/.agentgraph/app.pid"

server_up() {
    curl -s -f --max-time 2 "$PING_URL" > /dev/null 2>&1
}

echo "=== Conduction Launcher ==="
echo ""

if [ "$DO_STOP" = 1 ]; then
    if [ ! -f "$PID_PATH" ]; then
        echo "No PID file at $PID_PATH - nothing to stop."
        exit 0
    fi
    RECORDED_PID="$(cat "$PID_PATH")"
    if kill -0 "$RECORDED_PID" 2>/dev/null; then
        kill "$RECORDED_PID"
        echo "Stopped Conduction (PID $RECORDED_PID)."
    else
        echo "PID $RECORDED_PID is not running; clearing stale PID file."
    fi
    rm -f "$PID_PATH"
    exit 0
fi

echo "Checking if server is already running..."
if server_up; then
    echo "  Server is already running at $BASE_URL"
else
    echo "  Server not running, will start it..."

    mkdir -p "$SCRIPT_DIR/.agentgraph"

    # Clear a stale PID file so --stop never targets a recycled PID.
    if [ -f "$PID_PATH" ] && ! kill -0 "$(cat "$PID_PATH")" 2>/dev/null; then
        rm -f "$PID_PATH"
    fi

    PYTHON_EXE="$SCRIPT_DIR/venv/bin/python"
    APP_SCRIPT="$SCRIPT_DIR/app.py"

    if [ ! -x "$PYTHON_EXE" ]; then
        echo "Error: Virtual environment not found at $PYTHON_EXE" >&2
        echo "Please run install.sh first." >&2
        exit 1
    fi

    # Keep one previous run's log for post-mortem; the redirect below truncates.
    if [ -f "$LOG_PATH" ]; then mv -f "$LOG_PATH" "$LOG_PATH.prev"; fi

    echo "Starting Conduction server..."
    echo "  Host: $HOST"
    echo "  Port: $PORT"
    echo "  Logs: $LOG_PATH"

    CONDUCTION_PORT="$PORT" CONDUCTION_HOST="$HOST" \
        nohup "$PYTHON_EXE" "$APP_SCRIPT" > "$LOG_PATH" 2>&1 &
    SERVER_PID=$!

    echo "$SERVER_PID" > "$PID_PATH"
    echo "  Process ID: $SERVER_PID"

    echo "  Waiting up to ${MAX_WAIT}s for server to be ready..."
    READY=false
    ELAPSED=0
    while [ "$ELAPSED" -lt "$((MAX_WAIT * 2))" ]; do
        if ! kill -0 "$SERVER_PID" 2>/dev/null; then
            echo ""
            echo "Error: Server process exited immediately" >&2
            echo "Check logs at: $LOG_PATH" >&2
            exit 1
        fi
        sleep 0.5
        ELAPSED=$((ELAPSED + 1))
        if server_up; then READY=true; break; fi
    done

    if [ "$READY" = false ]; then
        echo ""
        echo "Error: Server did not start within $MAX_WAIT seconds" >&2
        echo "Check logs at: $LOG_PATH" >&2
        exit 1
    fi

    echo "  Server is ready!"
fi

if [ "$NO_BROWSER" != 1 ]; then
    echo ""
    echo "Opening browser at $BASE_URL ..."
    if command -v xdg-open > /dev/null 2>&1; then
        xdg-open "$BASE_URL" 2>/dev/null || true
    elif command -v open > /dev/null 2>&1; then
        open "$BASE_URL" 2>/dev/null || true
    else
        echo "  (Could not detect browser command, please open manually)"
    fi
fi

echo ""
echo "=== Conduction is running ==="
echo ""
echo "Access the interface at: $BASE_URL"
echo "Stop it with: ./conduction.sh --stop"
echo ""
