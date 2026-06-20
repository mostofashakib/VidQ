# VidQ

**A self-hosted video toolkit.** Download videos from any URL, convert and scale local files, combine multiple clips, and burn English subtitles into any video — all from a clean web interface.

---

## What You Can Do

| Page | What it does |
|------|-------------|
| **Download** | Paste a URL — VidQ navigates the page like a human, starts playback, and downloads the video |
| **Convert** | Upload a local file — scales to 720p, converts WebM to MP4 |
| **Combine** | Drop 2–20 clips — merges them into one 720p MP4 with crossfade transitions |
| **Translate** | Drop any video — transcribes it with Whisper, translates with an LLM, burns English subtitles |

Every page has a live job queue: submit as many jobs as you want, watch them move through queued → processing → done, cancel any job mid-flight, and download completed files immediately.

---

## Quick Start

```bash
git clone https://github.com/mostofashakib/VidQ.git
cd VidQ
```

Create `backend/.env` (see [Configuration](#configuration) for all options):

```env
DATABASE_URL=sqlite:///./videos.db
CORS_ORIGINS=http://localhost:3000
OPENAI_API_KEY=sk-...          # needed for Download (vision) and Translate (Whisper)
OLLAMA_HOST=http://127.0.0.1:11434  # needed for Translate (subtitle translation)
```

```bash
./run.sh
```

Open [http://localhost:3000](http://localhost:3000).

---

## Prerequisites

| Requirement | Notes |
|-------------|-------|
| Python 3.10+ | |
| Node.js 18+ | The startup script prefers Node 20 via nvm if available |
| [uv](https://github.com/astral-sh/uv) | Fast Python package manager — installed automatically if missing |
| ffmpeg | Bundled via `imageio-ffmpeg`; no system install needed |
| At least one LLM provider | OpenAI, Anthropic, OpenRouter, or [Ollama](https://ollama.com) running locally |

For the **Translate** feature specifically:
- **Transcription**: an OpenAI API key (for `whisper-1`) — or a local `openai-whisper` install
- **Translation**: Ollama running locally (default), or any other configured LLM provider

---

## Configuration

All settings go in `backend/.env`. Only `DATABASE_URL` is required; everything else has a sensible default or is feature-gated.

### Core

| Variable | Default | Description |
|----------|---------|-------------|
| `DATABASE_URL` | *(required)* | SQLite: `sqlite:///./videos.db`. Postgres also works. |
| `CORS_ORIGINS` | `""` | Comma-separated allowed origins, e.g. `http://localhost:3000` |
| `BASE_URL` | `http://localhost:8000` | Backend URL used for generating download links |
| `TEMP_STORAGE_DIR` | `backend/temp_storage` | Where temporary files are written during processing |

### Authentication

Authentication is opt-in. Leave both unset to run without a password.

| Variable | Default | Description |
|----------|---------|-------------|
| `AUTH_ENABLED` | `false` | Set to `true` to require a password |
| `APP_PASSWORD` | `""` | The password users enter at the login screen |

### LLM Providers

VidQ uses LLMs in two places: **navigation** (clicking play, extracting metadata during Download) and **subtitle translation** (Translate page). These are configured independently.

| Variable | Default | Description |
|----------|---------|-------------|
| `LLM_PROVIDER` | `ollama` | Navigation LLM: `ollama` \| `openai` \| `anthropic` \| `openrouter` \| `""` (auto-fallback) |
| `TRANSLATE_LLM_PROVIDER` | `ollama` | Translation LLM: same options. Defaults to local Ollama. |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama server URL |
| `OLLAMA_MODEL` | `gemma4:26b` | Model for both navigation and translation when using Ollama |
| `OPENAI_API_KEY` | `""` | Required if using OpenAI for navigation or translation |
| `ANTHROPIC_API_KEY` | `""` | Required if using Anthropic |
| `OPENROUTER_API_KEY` | `""` | Required if using OpenRouter |
| `OPENROUTER_MODEL` | `google/gemma-4-31b-it:free` | OpenRouter model to use |

When `LLM_PROVIDER` is blank, VidQ tries all configured providers in order and remembers which one works.

### Transcription (Translate feature)

| Variable | Default | Description |
|----------|---------|-------------|
| `TRANSCRIPTION_PROVIDER` | `openai_whisper` | `openai_whisper` (API) or `local_whisper` (local `openai-whisper` library) |
| `TRANSCRIPTION_MODEL` | `whisper-1` | Whisper model name. For local: `tiny`, `base`, `small`, `medium`, `large` |

To use local Whisper instead of the API: `pip install openai-whisper` in the backend venv, then set `TRANSCRIPTION_PROVIDER=local_whisper` and `TRANSCRIPTION_MODEL=base` (or whichever size fits your hardware).

### Download / Browser

| Variable | Default | Description |
|----------|---------|-------------|
| `BROWSER_HEADLESS` | `true` | Set to `false` to watch the browser window while downloading (useful for debugging) |
| `BROWSER_PROFILE_DIR` | `backend/app/browser_profile` | Persistent browser profile — stores cookies and localStorage so repeat visits look like a returning human |
| `PROXY_URLS` | `""` | Comma-separated proxy list for Cloudflare evasion. Format: `http://user:pass@host:port` or `socks5://host:port` |

### Example `.env`

```env
# Required
DATABASE_URL=sqlite:///./videos.db
CORS_ORIGINS=http://localhost:3000

# LLM — at least one provider needed for Download; Ollama needed for Translate
OPENAI_API_KEY=sk-...
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=qwen3.5:latest

# LLM routing
LLM_PROVIDER=                    # blank = auto-fallback; or: ollama | openai | anthropic | openrouter
TRANSLATE_LLM_PROVIDER=ollama    # default: use local Ollama for subtitle translation

# Transcription
TRANSCRIPTION_PROVIDER=openai_whisper   # or: local_whisper
TRANSCRIPTION_MODEL=whisper-1

# Auth (optional)
AUTH_ENABLED=false
APP_PASSWORD=

# Browser
BROWSER_HEADLESS=true
```

---

## How It Works

### Download Pipeline

Downloading a video URL runs through up to three stages:

**Stage 0 — Direct fast-path**
If the URL points straight to a video file or a minimal wrapper, VidQ downloads it via `yt-dlp` or `curl` without launching a browser.

**Stage 1 — Browser + Agentic Interaction**
VidQ opens the page in a headless Chromium browser and intercepts network traffic to find HLS manifests and video streams (including tokenized URLs like `playlist.m3u8?token=abc`). If the video hasn't auto-played, it runs a layered interaction loop to start it:

1. MediaSession API / `video.play()` / fullscreen
2. Accessibility heuristics — dismisses cookie banners, skip-ad buttons, age gates, play overlays
3. LLM-guided clicks — sends a screenshot + ARIA tree + cleaned HTML to the configured LLM to identify and click the play button
4. Popup retry handling — re-clicks through ad interruptions up to 10 times

A strategy cache remembers what worked on each domain so later visits skip straight to the successful approach.

Once the video is playing, VidQ downloads via `ffmpeg` or falls back to `yt-dlp` for tokenized/encrypted HLS streams.

**Stage 2 — MediaRecorder fallback**
For DRM-adjacent content and blob URLs, VidQ reloads the page in a fresh context, locates the `<video>` element (across iframes), injects a `MediaRecorder` into that frame's execution context, and records in real time. The recording stops automatically when the detected video duration is reached (instead of always running to the 3-hour cap). Blank or static captures are detected and rejected.

All videos are scaled to **720p** with Lanczos + libx264 CRF 18 after download.

Up to **5 downloads** run in parallel; additional jobs queue automatically.

### Convert Pipeline

Files upload directly to the backend. A 5-worker thread pool scales each video to 720p and converts WebM to MP4 via ffmpeg.

### Combine Pipeline

Uploaded clips are normalized to 720p individually, then joined with ffmpeg's `xfade` filter (0.5s crossfade overlap). Progress updates per clip during normalize and by percentage during merge.

### Translate Pipeline

1. **Audio extraction** — ffmpeg extracts a mono 16kHz WAV
2. **Transcription** — the configured `TRANSCRIPTION_PROVIDER` (Whisper API or local) produces timestamped SRT segments
3. **Translation** — segments are batched into ~2000-token chunks and sent to the configured `TRANSLATE_LLM_PROVIDER`. Each chunk is translated individually to preserve timing and avoid summarization
4. **Burn** — ffmpeg burns the translated subtitles into the video (white text, black outline, bottom-center, 720p output)

Up to **3 translate jobs** run in parallel; additional jobs queue automatically.

---

## Architecture

```
frontend/                     Next.js 15 (App Router)
  app/
    page.tsx                  Download page — URL input, job queue, video library
    upload/page.tsx           Convert page — file drop zone, job queue, video list
    combine/page.tsx          Combine page — multi-clip drop zone, job queue
    translate/page.tsx        Translate page — single-file drop zone, job queue
  src/components/
    Navbar.tsx                Sticky nav: Download · Convert · Combine · Translate

backend/                      FastAPI
  app/
    config.py                 All settings loaded from .env
    state.py                  Process-lifetime singletons (LLM managers, transcription adapter)
    routers/
      auth.py                 Optional password login, Bearer token validation
      video.py                Download jobs, video library, streaming
      upload.py               Convert/upload endpoint with progress tracking
      combine.py              Combine endpoint with polling
      translate.py            Translate endpoint with polling
    services/
      transcription.py        TranscriptionAdapter ABC — WhisperOpenAIAdapter | WhisperLocalAdapter
      llm_manager.py          LLMProvider ABC + OllamaProvider | OpenAIProvider | AnthropicProvider |
                              OpenRouterProvider + FallbackLLMManager (fallback chain + provider memory)
      queue.py                5-concurrent download runner with per-job threads, cancellation, phases
      upload_worker.py        720p scaling + WebM→MP4 conversion workers
      combine_worker.py       Per-clip normalize + xfade merge workers
      translate_worker.py     Audio extract → transcribe → translate → burn workers
      scraper/
        pipeline.py           Three-stage pipeline: direct → browser+agent → MediaRecorder
        playback.py           Agentic interaction loop, strategy cache, frame-aware helpers
        media.py              ffmpeg/yt-dlp download, ad URL filtering, duration guard
```

---

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

Built by [Variant Labs](https://www.vriantlabs.com) · [hello@vriantlabs.com](mailto:hello@vriantlabs.com)

</div>
