# VidQ

Self-hosted video automation for downloading, converting, combining, translating, trimming, and enhancing videos from one local web app.

VidQ is built for people who want a private video workstation they can clone, run, and use without hunting through dependency docs.

## Install

Runtime requirements:

- Python 3.10+
- Node.js 18+
- macOS or Linux
- At least one LLM provider for agentic Download and Translate workflows

Recommended setup:

```bash
git clone https://github.com/mostofashakib/VidQ.git
cd VidQ
./setup.sh
```

The setup script installs:

- `uv` if it is missing
- backend Python packages from `backend/requirements.txt`
- backend test packages from `backend/requirements-dev.txt`
- Playwright Chromium
- frontend packages from `frontend/package-lock.json`
- `realesrgan-ncnn-vulkan` on macOS for the Enhance feature
- `backend/.env` from `backend/.env.example` if it does not exist

Skip the Enhance dependency during setup:

```bash
SKIP_REAL_ESRGAN=1 ./setup.sh
```

Skip the browser download in constrained environments:

```bash
SKIP_PLAYWRIGHT=1 ./setup.sh
```

## Quick Start

Start the app:

```bash
./run.sh
```

Open:

```text
http://localhost:3000
```

`./run.sh` runs setup first, clears temporary output, starts FastAPI on port `8000`, and starts Next.js on port `3000`.

Skip setup on later runs:

```bash
SKIP_SETUP=1 ./run.sh
```

Stop the app with `Ctrl+C`.

## Configuration

Edit:

```text
backend/.env
```

Minimal local config:

```env
DATABASE_URL=sqlite:///./videos.db
CORS_ORIGINS=http://localhost:3000
BASE_URL=http://localhost:8000
```

Use Ollama locally:

```env
LLM_PROVIDER=ollama
TRANSLATE_LLM_PROVIDER=ollama
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=gemma4:26b
```

Use hosted LLMs:

```env
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
OPENROUTER_API_KEY=
```

Optional password gate:

```env
AUTH_ENABLED=true
APP_PASSWORD=change-me
```

Enhance binary override:

```env
REAL_ESRGAN_BIN=/Users/you/.local/opt/realesrgan-ncnn-vulkan/realesrgan-ncnn-vulkan
```

## Features

- **Download** - paste a URL; VidQ opens the page, starts playback, finds the stream, and downloads the video.
- **Convert** - upload a video; VidQ scales to 720p and converts WebM to MP4.
- **Combine** - drop 2-20 clips; VidQ merges them into one MP4 with crossfades.
- **Translate** - upload a video; VidQ transcribes, translates, and burns English subtitles.
- **Trim** - upload a video; VidQ exports the selected timeline segment.
- **Enhance** - upload low-quality footage; VidQ uses Real-ESRGAN to clean and upscale it.

Every feature has a job queue with progress, cancellation, and download links.

## Dependency Notes

### Real-ESRGAN

Homebrew does not provide `realesrgan-ncnn-vulkan`.

On macOS, run:

```bash
./setup.sh
```

The script installs the official Real-ESRGAN release binary into:

```text
~/.local/opt/realesrgan-ncnn-vulkan
```

It also links the binary into:

```text
~/.local/bin/realesrgan-ncnn-vulkan
```

When `backend/.env` exists, it writes `REAL_ESRGAN_BIN` automatically.

### Whisper

VidQ defaults to local `faster-whisper`:

```env
TRANSCRIPTION_PROVIDER=faster_whisper
TRANSCRIPTION_MODEL=large-v3-turbo
```

To use OpenAI Whisper instead:

```env
TRANSCRIPTION_PROVIDER=openai_whisper
TRANSCRIPTION_MODEL=whisper-1
OPENAI_API_KEY=sk-...
```

### Playwright

Setup installs Chromium with:

```bash
uv run playwright install chromium
```

This is required for the Download page's browser automation.

## Troubleshooting

### `realesrgan-ncnn-vulkan not found`

Run:

```bash
./setup.sh
```

Then restart:

```bash
./run.sh
```

### `Missing required environment variable: DATABASE_URL`

Create the env file:

```bash
cp backend/.env.example backend/.env
```

### Node version issues

Use Node 20 if your local Node version causes Next.js problems:

```bash
nvm install 20
nvm use 20
```

### Reinstall everything

```bash
FORCE_INSTALL=1 ./setup.sh
```

## Commands

```bash
./setup.sh
./run.sh
./kill.sh
backend/.venv/bin/pytest
cd frontend && npm run build
```

## Project Layout

```text
frontend/                    Next.js app
  app/                       Pages for Download, Convert, Combine, Translate, Trim, Enhance
  src/components/            Shared UI

backend/                     FastAPI app
  app/routers/               API routes
  app/services/              Workers, browser automation, media pipelines
  tests/                     Backend tests

setup.sh                     One-command project setup, including macOS Real-ESRGAN install
```

## How It Works

Download uses a staged pipeline:

1. Try direct video extraction with `yt-dlp`, `curl`, and `ffmpeg`.
2. Launch Chromium, inspect network traffic, and use heuristics plus an LLM-guided click loop to start playback.
3. Fall back to MediaRecorder for blob and DRM-adjacent streams when direct download fails.

Enhance uses a chunked Real-ESRGAN pipeline:

1. Split the video into 60-second chunks.
2. Extract frames from one chunk at a time.
3. Upscale frames with `realesrgan-ncnn-vulkan`.
4. Reassemble chunks and mux the original audio.

This keeps disk usage bounded during long Enhance jobs.

## License

MIT. See [LICENSE](LICENSE).

Built by [Variant Labs](https://www.vriantlabs.com).
