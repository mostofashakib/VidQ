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

- system tools it can install automatically (`curl`, `unzip`, Node.js/npm)
- `uv` if it is missing
- backend Python packages from `backend/requirements.txt`
- backend test packages from `backend/requirements-dev.txt`
- Playwright Chromium
- Playwright Linux system libraries when running on Linux
- frontend packages from `frontend/package-lock.json`
- Real-ESRGAN ncnn and Python backends for the Enhance feature
- Real-ESRGAN model weights for the Python fallback backend
- `backend/.env` from `backend/.env.example` if it does not exist

Skip system package installation and fail with instructions instead:

```bash
SKIP_SYSTEM_DEPS=1 ./setup.sh
```

Skip the ncnn Enhance dependency during setup:

```bash
SKIP_REAL_ESRGAN=1 ./setup.sh
```

Skip the Python Real-ESRGAN fallback:

```bash
SKIP_PYTHON_REALESRGAN=1 ./setup.sh
```

Skip the browser download in constrained environments:

```bash
SKIP_PLAYWRIGHT=1 ./setup.sh
```

Skip Playwright Linux system packages:

```bash
SKIP_PLAYWRIGHT_SYSTEM_DEPS=1 ./setup.sh
```

Reinstall managed dependencies:

```bash
FORCE_INSTALL=1 ./setup.sh
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

Enhance backend override:

```env
REAL_ESRGAN_BACKEND=auto
REAL_ESRGAN_BIN=/Users/you/.local/opt/realesrgan-ncnn-vulkan/realesrgan-ncnn-vulkan
REAL_ESRGAN_PYTHON=/path/to/backend/.realesrgan-venv/bin/python
REAL_ESRGAN_MODEL_PATH=/path/to/backend/models/realesrgan/RealESRGAN_x4plus.pth
```

## Features

- **Download** - paste a URL; VidQ opens the page, starts playback, finds the stream, and downloads the video.
- **Convert** - upload a video; VidQ scales to 720p and converts WebM to MP4.
- **Combine** - drop 2-20 clips; VidQ merges them into one MP4 with crossfades.
- **Translate** - upload a video; VidQ transcribes, translates, and burns English subtitles.
- **Trim** - upload a video; VidQ exports the selected timeline segment.
- **Enhance** - upload low-quality footage; VidQ runs parallel chunked Real-ESRGAN upscaling with safe fallback handling.

Every feature has a job queue with progress, cancellation, and download links.

## Dependency Notes

### Real-ESRGAN

Homebrew does not provide `realesrgan-ncnn-vulkan`. VidQ installs two Enhance backends:

- **ncnn Vulkan** - fast, but can crash on some macOS GPU/Vulkan setups.
- **Python Real-ESRGAN** - slower, but uses the upstream PyTorch implementation as a fallback.

Run:

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

When `backend/.env` exists, setup writes `REAL_ESRGAN_BIN`, `REAL_ESRGAN_PYTHON`, and `REAL_ESRGAN_MODEL_PATH` automatically.

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

## Architecture

VidQ is a local full-stack video workstation:

```text
Browser UI
  ↓
Next.js app routes
  ↓
FastAPI routers
  ↓
Background worker queues
  ↓
Media tools, browser automation, LLM providers, and local storage
```

- **Frontend** - Next.js App Router pages in `frontend/app` handle upload forms, progress polling, cancellation, result playback, and downloads.
- **API layer** - FastAPI routers in `backend/app/routers` validate requests, save uploads, create jobs, and expose job status endpoints.
- **Workers** - Service workers in `backend/app/services` run long video tasks outside request handlers so the UI stays responsive.
- **Queue runtime** - Shared worker helpers centralize job state, cancellation, cleanup, and global concurrency limits.
- **Media layer** - `imageio-ffmpeg`, `yt-dlp`, and Playwright handle downloading, probing, converting, trimming, combining, subtitles, and final MP4 output.
- **AI layer** - Download can use LLM-guided browser navigation; Translate uses Whisper plus an LLM provider; Enhance uses Real-ESRGAN.
- **Storage** - SQLite stores saved video metadata, while generated files live under `backend/temp_storage` and are served back through FastAPI.

## How It Is Built

- **Download** starts with direct extraction, then falls back to Playwright browser automation and MediaRecorder-style capture when needed.
- **Convert** saves uploads, normalizes video to 720p MP4, and exposes the finished file in the uploaded video library.
- **Combine** accepts ordered clips, runs one high-quality ffmpeg pass, outputs 720p MP4, and preserves aspect ratio with padding.
- **Translate** extracts audio, transcribes locally with `faster-whisper` or OpenAI Whisper, translates text, creates subtitles, and burns them into the video.
- **Trim** lets the UI choose start/end timestamps, then ffmpeg exports only that segment.
- **Enhance** splits long videos into chunks, runs Real-ESRGAN upscaling with ncnn/Python fallback, then reassembles video and audio.
- **Setup** installs Python, Node, browser, frontend, backend, and Enhance dependencies so a new clone can run with `./run.sh`.

## How It Works

Download uses a staged pipeline:

1. Try direct video extraction with `yt-dlp`, `curl`, and `ffmpeg`.
2. Launch Chromium, inspect network traffic, and use heuristics plus an LLM-guided click loop to start playback.
3. Fall back to MediaRecorder for blob and DRM-adjacent streams when direct download fails.

Enhance uses a parallel chunked Real-ESRGAN pipeline:

1. Split the video into 60-second chunks.
2. Process up to 5 chunks in parallel across the shared global worker capacity.
3. Extract frames, upscale them with Real-ESRGAN ncnn, and fall back to Python Real-ESRGAN when ncnn crashes.
4. Preserve chunk order, reassemble the video, and mux the original audio.

This keeps disk usage bounded during long Enhance jobs while fully using available worker capacity without starving other queued jobs.

## License

MIT. See [LICENSE](LICENSE).

Built by [Variant Labs](https://www.vriantlabs.com).
