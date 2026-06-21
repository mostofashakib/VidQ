# ── Stage 1: Build Next.js frontend ──────────────────────────────────────────
FROM node:20-slim AS frontend-builder

WORKDIR /app/frontend
COPY frontend/package*.json ./
RUN npm ci --prefer-offline
COPY frontend/ ./

# NEXT_PUBLIC_API_URL is baked into the client bundle at build time.
# Pass your backend's public URL at build time:
#   docker build --build-arg NEXT_PUBLIC_API_URL=https://api.example.com .
ARG NEXT_PUBLIC_API_URL=http://localhost:8000
ENV NEXT_PUBLIC_API_URL=${NEXT_PUBLIC_API_URL}

RUN npm run build

# ── Stage 2: Runtime image ────────────────────────────────────────────────────
FROM python:3.12-slim

# ffmpeg for media processing; curl + ca-certificates for yt-dlp and Playwright downloads;
# Node.js 20 (matches local dev) to run the Next.js production server.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl ca-certificates \
    && curl -fsSL https://deb.nodesource.com/setup_20.x | bash - \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# uv for fast Python package installation
RUN pip install uv --no-cache-dir

WORKDIR /app

# Install backend Python dependencies before copying source so this layer is cached
# when only source files change.
COPY backend/requirements.txt backend/requirements.txt
RUN uv pip install --system --no-cache -r backend/requirements.txt

# Install Playwright Chromium and all its Linux system dependencies.
# BROWSER_HEADLESS must be true (the default) in cloud deployments — there is no display.
RUN playwright install chromium --with-deps && rm -rf /var/lib/apt/lists/*

# Copy built frontend assets from stage 1
COPY --from=frontend-builder /app/frontend/.next         frontend/.next
COPY --from=frontend-builder /app/frontend/public        frontend/public
COPY --from=frontend-builder /app/frontend/node_modules  frontend/node_modules
COPY --from=frontend-builder /app/frontend/package.json  frontend/package.json
COPY --from=frontend-builder /app/frontend/next.config.ts frontend/next.config.ts

# Copy backend source (requirements already installed above)
COPY backend/ backend/

# Whisper model weights (~1.5 GB) are downloaded to this directory on first Translate job.
# Mount a named volume here to persist weights across container restarts:
#   -v vidq-whisper-models:/app/backend/models/whisper
# temp_storage holds in-progress and completed media files — also mount a volume
# if you need files to survive redeploys.
RUN mkdir -p backend/models/whisper backend/temp_storage

COPY docker-entrypoint.sh /docker-entrypoint.sh
RUN chmod +x /docker-entrypoint.sh

# Real-ESRGAN (Enhance feature) is NOT included — the ncnn binary requires Vulkan GPU
# drivers that are not present in standard cloud VMs. Set REAL_ESRGAN_BACKEND=python
# and install the Python backend separately if you need Enhance in the cloud.

EXPOSE 8000 3000

ENTRYPOINT ["/docker-entrypoint.sh"]
