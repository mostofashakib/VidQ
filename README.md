# VidQ

**Paste a URL. VidQ downloads the video.** No browser extensions, no site-specific configs — it works by navigating the page like a human, handling cookie banners, ad overlays, and custom video players automatically. Works on standard video sites as well as streaming platforms like vidara.so and lulustream that embed players inside iframes and serve HLS streams with token-based URLs.

---

## How It Works

VidQ runs a multi-stage pipeline inside a headless Chromium browser:

### Stage 0 — Direct Embed Fast-Path
If the URL points directly to a video file or a minimal HTML wrapper around one, VidQ downloads it immediately via `curl`/`yt-dlp` — no Playwright or LLM invocation needed.

### Stage 1 — Fast Pass (Network Sniff + Agentic Interact)
1. Opens the page and intercepts both **requests** (by file extension in the URL path) and **responses** (by `Content-Type` header). This catches HLS manifests like `playlist.m3u8?token=abc...` whose full URL doesn't end in `.m3u8`.
2. Detects whether the video **auto-started** (no click needed) and skips the interaction loop if so.
3. If playback hasn't started, runs a **3-layer agentic loop** to start it:
   - **Layer 1** — Pure JS heuristics dismiss cookie banners, skip-ad buttons, countdowns, and age-gates. Calls `video.play()` directly.
   - **Layer 2** — Takes a screenshot + page HTML (including content from child iframes) and asks an LLM vision model which element to click next.
   - **Layer 3** — After each click, force-play and heuristic selectors re-run across all frames to confirm playback started.
4. Identifies the **main video** (largest on-screen area) across the main frame and all child iframes, then downloads it via `ffmpeg`. Falls back to **yt-dlp** for tokenized HLS/DASH streams that `ffmpeg` can't reassemble.

### Stage 2 — Heavy Pass (MediaRecorder Fallback)
If Stage 1 can't produce a downloadable file (DRM-adjacent content, blob URLs, or encrypted segments), VidQ:
1. Reloads the page in a fresh context.
2. Locates which frame (main or iframe) actually holds the `<video>` element.
3. Injects a `MediaRecorder` into that frame's execution context — this is required because `captureStream()` must run inside the same frame as the video, not the parent page.
4. Records in real time (up to the video's own reported duration, capped at 3 hours).
5. Converts the WebM capture to MP4 automatically.

Videos are stored locally — no expiring CDN links.

---

## Features

### Agentic Navigation
- Works on any website without per-site configuration
- Auto-play detection skips the interaction loop when the video starts immediately
- LLM vision model guides click decisions when JS heuristics aren't enough
- Interaction loop searches the main frame **and all child iframes** for play buttons and video elements
- Supports **OpenAI (GPT-4o)**, **Anthropic (Claude Haiku)**, and **Ollama** — tries them in order and remembers the last working provider

### Streaming Platform Support
- Path-based URL matching catches HLS manifest URLs with token query strings (`?token=abc&expires=...`)
- Response `Content-Type` detection captures manifests that have non-standard URL paths
- Cross-frame interaction: click handlers and play detection operate in child iframe contexts
- Frame-targeted MediaRecorder: recording setup runs inside the iframe's own JS context
- yt-dlp fallback handles tokenized and encrypted HLS/DASH that ffmpeg can't reassemble

### Smart Video Detection
- Selects the video element with the largest screen area across main frame and iframes, ignoring ads and thumbnail previews
- Filters ad URLs by domain blocklist (`doubleclick.net`, `adnxs.com`, `tsyndicate.com`, etc.) and dimension patterns (`440x250.mp4`)
- **Duration guard** — if a downloaded file is less than half the duration reported by the page, VidQ discards it as a pre-roll ad and tries the next candidate

### Quality Processing
- All videos are scaled to **720p** using Lanczos + libx264 CRF 18 (upscales if below, downscales if above)
- MediaRecorder captures are converted from WebM to MP4 automatically

### File Upload
- Upload local video files directly — processed through the same quality pipeline
- Per-upload job tracking with progress and cancellation support

### Job Queue
- Add as many URLs as you want — processed by a 5-worker thread pool with overflow queuing
- Live status updates: `queued → fast_pass → heavy_pass_waiting → heavy_pass_recording → done`
- Recording timer shown during `heavy_pass_recording` so you know how long capture has been running
- **Cancel any job at any stage** — queued jobs are removed immediately; in-progress jobs stop within ~300ms

### Video Library
- Saved videos stream directly from local storage — watch in-browser or download with one click
- Tag videos with a category at add-time and filter by category in the grid
- Infinite scroll with deduplication (same URL or title won't be saved twice)

### Security
- URL validation blocks SSRF attacks (private IPs, loopback addresses, non-HTTP schemes)
- Optional password authentication with timing-safe token comparison
- Auth can be disabled entirely for local-only use

---

## Architecture

```
frontend/   Next.js 15 (App Router) — video grid, download queue panel, in-browser player
backend/    FastAPI
  routers/
    auth.py           Password login, Bearer token validation
    video.py          Add video, list, delete, queue status, file serving
    upload.py         Direct file upload with per-job progress tracking
  services/
    scraper/
      pipeline.py     Three-stage pipeline: embed fast-path → fast pass → MediaRecorder fallback
      playback.py     Agentic interact loop, JS heuristics, force-play, frame-aware helpers
      media.py        ffmpeg/yt-dlp download, quality scaling, ad URL filtering
      html.py         HTML cleaning for LLM context
    llm_manager.py    Provider abstraction + fallback chain (OpenAI → Anthropic → Ollama)
    queue.py          5-worker thread pool with per-job cancellation and phase tracking
    upload_worker.py  Upload processing worker
    video_utils.py    Quality scaling via ffmpeg
  db.py     SQLite via SQLAlchemy
```

---

## Getting Started

### Prerequisites

- Python 3.10+
- Node.js 18+
- [uv](https://github.com/astral-sh/uv)
- At least one of: an OpenAI API key, an Anthropic API key, or [Ollama](https://ollama.com) running locally

### Setup

**1. Clone**
```bash
git clone https://github.com/mostofashakib/VidQ.git
cd VidQ
```

**2. Configure the backend**

Create `backend/.env`:
```env
OPENAI_API_KEY=sk-...                    # optional
ANTHROPIC_API_KEY=sk-ant-...             # optional
OLLAMA_HOST=http://127.0.0.1:11434       # optional, defaults to this
LLM_PROVIDER=                            # leave blank for auto-fallback, or set "openai" / "anthropic" / "ollama"
AUTH_PASSWORD=yourpassword               # leave blank to disable auth
CORS_ORIGINS=http://localhost:3000
TEMP_STORAGE_DIR=temp_storage
```

**3. Start**
```bash
./run.sh
```

Open [http://localhost:3000](http://localhost:3000).

---

## Usage

1. Paste a video page URL (any site, including HLS/iframe-embedded streaming platforms).
2. Enter a category name.
3. Click **Add Video**.
4. Watch the job move through `queued → fast_pass → heavy_pass_waiting → heavy_pass_recording → done` in the Downloads panel.
5. Once done, the video appears in the grid — click ▶ to play in-browser, or the download icon to save to disk.

To add a local file instead, use the **Upload Video** option in the nav.

---

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

Built by [Variant Labs](https://www.vriantlabs.com) · [hello@vriantlabs.com](mailto:hello@vriantlabs.com)

</div>
