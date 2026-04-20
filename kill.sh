#!/bin/bash

# Function to kill existing processes on specific ports
kill_port() {
    local PORT=$1
    local PID=$(lsof -ti tcp:${PORT} || true)
    if [ ! -z "$PID" ]; then
        echo "Killing process on port $PORT (PID $PID)"
        kill -9 $PID
    else
        echo "No process running on port $PORT"
    fi
}

echo "Cleaning up VideoSearch processes..."
kill_port 8000
kill_port 3000
echo "Cleanup complete."
