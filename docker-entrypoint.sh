#!/bin/bash
set -e

cd /app/backend
uvicorn app.main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

cd /app/frontend
npm run start -- -p 3000 &
FRONTEND_PID=$!

cleanup() {
    kill "$BACKEND_PID" "$FRONTEND_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

echo "VidQ backend  → http://0.0.0.0:8000"
echo "VidQ frontend → http://0.0.0.0:3000"

# Exit (and restart the container) if either process dies
wait "$BACKEND_PID" "$FRONTEND_PID"
