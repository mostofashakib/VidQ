# VidQ

**Paste a URL. VidQ downloads the video.** No browser extensions, no site-specific configs — it works by navigating the page like a human, handling cookie banners, ad overlays, and custom video players automatically.

---

## How It Works

VidQ runs a two-pass pipeline inside a headless Chromium browser:

### Pass 1 — Fast Download
1. Opens the page and intercepts network requests for video files (`.mp4`, `.m3u8`, `.webm`, etc.)
2. Runs a **3-layer agentic loop** to get the video playing:
   - **Layer 1** — Pure JS heuristics dismiss cookie banners, skip-ad buttons, countdowns, and age-gates. Calls `video.play()` directly.
   - **Layer 2** — Takes a screenshot + page HTML and asks an LLM vision model which element to click next (play button, close overlay, etc.)
   - **Layer 3** — After each click, force-play and heuristic selectors re-run to confirm playback started.
3. Identifies the **main video** — the one with the largest on-screen dimensions — and downloads it via `ffmpeg` with the correct `Referer` and `User-Agent` headers so CDN session tokens are honored.

### Pass 2 — MediaRecorder Fallback
If `ffmpeg` can't download the file (e.g. DRM-adjacent content or blob-URL streams), VidQ injects a `MediaRecorder` into the page and captures the stream in real time, then converts the result to MP4.

Videos are stored locally — no expiring CDN links.

---

## Features

### Agentic Navigation
- Works on any website without per-site configuration
- LLM vision model guides click decisions when JS heuristics aren't enough
- Supports **OpenAI (GPT-4o)**, **Anthropic (Claude Haiku)**, and **Ollama** — tries them in order and remembers the last working provider

### Smart Video Detection
- Selects the video element with the largest screen area, ignoring ads and thumbnail previews
- Filters ad URLs by domain blocklist (`doubleclick.net`, `adnxs.com`, etc.) and dimension patterns (`440x250.mp4`)
- **Duration guard** — if a downloaded file is less than half the duration reported by the page, VidQ discards it as a pre-roll ad and tries the next candidate

### Quality Processing
- All videos are scaled to **720p** using Lanczos + libx264 CRF 18 (upscales if below, downscales if above)
- MediaRecorder captures are converted from WebM to MP4 automatically

### Job Queue
- Add as many URLs as you want — they process one at a time in the background
- Live status updates: `extracting → queued → processing → done`
- **Cancel any job at any stage** — queued jobs are removed immediately; in-progress jobs stop at the next checkpoint (within ~300ms)

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
    auth.py         Password login, Bearer token validation
    video.py        Add video, list, delete, queue status, file serving
  services/
    scraper/
      pipeline.py   Two-pass Playwright pipeline (Fast Pass + MediaRecorder fallback)
      playback.py   Agentic interact loop, JS heuristics, force-play
      media.py      ffmpeg download, quality scaling, ad URL filtering
      html.py       HTML cleaning for LLM context
    llm_manager.py  Provider abstraction + fallback chain (OpenAI → Anthropic → Ollama)
    queue.py        Thread-safe background job queue with per-job cancellation
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

1. Paste a video page URL (any site).
2. Enter a category name.
3. Click **Add Video**.
4. Watch the job move through `extracting → queued → processing → done` in the Downloads panel.
5. Once done, the video appears in the grid — click ▶ to play in-browser, or the download icon to save to disk.

---

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

Built by [Variant Labs](https://www.vriantlabs.com) · [hello@vriantlabs.com](mailto:hello@vriantlabs.com)

</div>