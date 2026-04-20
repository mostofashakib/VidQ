#!/bin/bash
set -e

echo "Starting VideoSearch Application..."

# Function to kill existing processes on specific ports
kill_port() {
    local PORT=$1
    local PID=$(lsof -ti tcp:${PORT} || true)
    if [ ! -z "$PID" ]; then
        echo "Killing process on port $PORT (PID $PID)"
        kill -9 $PID
    fi
}

# 1. Kill any existing instances avoiding port conflicts
kill_port 8000
kill_port 3000

# 2. Setup and run Backend
echo "Setting up backend..."
cd backend

# Ensure uv is installed
if ! command -v uv &> /dev/null; then
    echo "uv is not installed. Installing uv..."
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

if [ ! -d ".venv" ]; then
    echo "Creating virtual environment using uv..."
    uv venv
fi

echo "Activating virtual environment..."
source .venv/bin/activate

echo "Creating requirements.txt if missing..."
if [ ! -f "requirements.txt" ]; then
    cat <<EOT > requirements.txt
fastapi[standard]
sqlalchemy
requests
beautifulsoup4
openai
python-dotenv
playwright
EOT
fi

echo "Installing backend dependencies using uv..."
uv pip install -r requirements.txt
playwright install chromium

echo "Starting backend..."
uvicorn app.main:app --host 0.0.0.0 --port 8000 &
BACKEND_PID=$!

cd ..

# 3. Setup and run Frontend
echo "Setting up frontend..."
cd frontend
if [ ! -d "node_modules" ]; then
    echo "Installing frontend dependencies..."
    npm install
fi

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
echo "VideoSearch Application is successfully running!"
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
