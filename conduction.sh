#!/usr/bin/env bash
# Launch Conduction

set -euo pipefail

# Determine script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Determine port from environment or default
PORT="${CONDUCTION_PORT:-8000}"
BASE_URL="http://127.0.0.1:$PORT"
PING_URL="$BASE_URL/api/ping"

echo "=== Conduction Launcher ==="
echo ""

# Check if server is already running
ALREADY_RUNNING=false
echo "Checking if server is already running..."
if curl -s -f --max-time 2 "$PING_URL" > /dev/null 2>&1; then
    ALREADY_RUNNING=true
    echo "  Server is already running at $BASE_URL"
else
    echo "  Server not running, will start it..."
fi

if [ "$ALREADY_RUNNING" = false ]; then
    # Ensure .agentgraph directory exists
    mkdir -p .agentgraph

    # Start the server
    PYTHON_EXE="$SCRIPT_DIR/venv/bin/python"
    APP_SCRIPT="$SCRIPT_DIR/app.py"
    LOG_PATH="$SCRIPT_DIR/.agentgraph/app.log"
    PID_PATH="$SCRIPT_DIR/.agentgraph/app.pid"

    if [ ! -f "$PYTHON_EXE" ]; then
        echo "Error: Virtual environment not found. Please run install.sh first."
        exit 1
    fi

    echo "Starting Conduction server..."
    echo "  Port: $PORT"
    echo "  Logs: $LOG_PATH"

    # Start the server in background
    CONDUCTION_PORT="$PORT" nohup "$PYTHON_EXE" "$APP_SCRIPT" > "$LOG_PATH" 2>&1 &
    SERVER_PID=$!

    # Record PID
    echo "$SERVER_PID" > "$PID_PATH"
    echo "  Process ID: $SERVER_PID"

    # Wait for server to be ready (max 30 seconds)
    echo "  Waiting for server to be ready..."
    MAX_WAIT=30
    WAITED=0
    READY=false

    while [ $WAITED -lt $MAX_WAIT ]; do
        sleep 1
        WAITED=$((WAITED + 1))

        if curl -s -f --max-time 2 "$PING_URL" > /dev/null 2>&1; then
            READY=true
            break
        fi
    done

    if [ "$READY" = false ]; then
        echo ""
        echo "Error: Server did not start within $MAX_WAIT seconds"
        echo "Check logs at: $LOG_PATH"
        exit 1
    fi

    echo "  Server is ready!"
fi

# Open browser (use xdg-open on Linux, open on macOS)
echo ""
echo "Opening browser at $BASE_URL ..."
if command -v xdg-open &> /dev/null; then
    xdg-open "$BASE_URL" 2>/dev/null || true
elif command -v open &> /dev/null; then
    open "$BASE_URL" 2>/dev/null || true
else
    echo "  (Could not detect browser command, please open manually)"
fi

echo ""
echo "=== Conduction is running ==="
echo ""
echo "Access the interface at: $BASE_URL"
echo ""
