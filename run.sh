#!/bin/bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Ensure project and helper binaries are visible to the backend.
export PATH="${ROOT_DIR}/backend/.venv/bin:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"

echo "Starting VidQ..."

# Function to kill existing processes on specific ports
kill_port() {
    local PORT=$1
    local PID
    PID=$(lsof -ti tcp:${PORT} || true)
    if [ -n "$PID" ]; then
        echo "Killing process on port $PORT (PID $PID)"
        kill -9 $PID
    fi
}

if [ "${SKIP_SETUP:-0}" != "1" ]; then
    "${ROOT_DIR}/setup.sh"
fi

cd "$ROOT_DIR"

echo "Clearing temp_storage..."
mkdir -p backend/temp_storage
rm -rf backend/temp_storage/*

kill_port 8000
kill_port 3000

cd backend
echo "Starting backend..."
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

cd ..

cd frontend

# Load NVM and use Node 20 to avoid Next.js localStorage crashes on Node 22/25
if [ -s "$HOME/.nvm/nvm.sh" ]; then
    export NVM_DIR="$HOME/.nvm"
    source "$NVM_DIR/nvm.sh"
    nvm install 20
    nvm use 20
fi

echo "Starting frontend..."
npm run dev -- -p 3000 &
FRONTEND_PID=$!

cd ..

echo ""
echo "VidQ is running."
echo "Backend: http://localhost:8000 (PID: $BACKEND_PID)"
echo "Frontend: http://localhost:3000 (PID: $FRONTEND_PID)"
echo "Press Ctrl+C to stop both applications."

# Keep script active and handle cleanup on exit
cleanup() {
    echo "Terminating applications..."
    kill -9 $BACKEND_PID $FRONTEND_PID 2>/dev/null || true
    exit 0
}
trap cleanup SIGINT SIGTERM

wait $BACKEND_PID $FRONTEND_PID
