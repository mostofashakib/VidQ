# VidQ

An agentic video downloader that download any video from any website. Paste a URL, and VidQ navigates the page autonomously, handling cookie banners, ad overlays, and custom video players and then downloads and saves the video locally so you can play it anytime.

---

## How it works

1. You paste a URL and a category into the UI.
2. VidQ opens the page in a headless Chromium browser.
3. A 3-layer agentic loop runs to get the video playing:
   - **Layer 1 (no LLM)** — JS heuristics dismiss consent banners, skip-ad buttons, and age-gates, then call `video.play()` directly on the largest video element.
   - **Layer 2 (LLM vision)** — A screenshot + the page HTML are sent to the vision model (OpenAI / Anthropic / Ollama). The model returns the CSS selector of the next thing to click (play button, close button, etc.).
   - **Layer 3 (recovery)** — After each click, force-play and heuristic selectors run again to confirm playback started.
4. Once the video is playing, VidQ identifies the **main video** — the one with the largest screen dimensions — and reads its `currentSrc` URL.
5. It downloads the video directly via **ffmpeg** with the correct `Referer` and `User-Agent` headers so CDN session tokens are honoured.
6. If ffmpeg can't download it (e.g. DRM-adjacent content), a **MediaRecorder fallback** captures the stream in real time.
7. The saved file is stored locally and streamed back to the browser on demand — no expiring CDN links.

---

## Features

| Feature | Detail |
|---|---|
| **Agentic navigation** | LLM vision + HTML guide click decisions on any site — no per-site config |
| **Smart main video detection** | Selects the largest video element by screen area, ignoring ads and related-video thumbnails |
| **Ad URL filtering** | Blocks known ad-network domains and dimension-pattern URLs (e.g. `440x250.mp4`) at the network intercept layer |
| **ffmpeg direct download** | Downloads with `Referer` + `User-Agent` headers — works on most CDNs without browser session hacks |
| **MediaRecorder fallback** | Captures the stream live for any video a browser can play |
| **Parallel job queue** | Queue as many URLs as you want; they process one at a time in the background |
| **Cancellation** | Cancel any job at any stage — HTTP request, queued, or mid-recording |
| **LLM fallback chain** | Tries OpenAI → Anthropic → Ollama in order; remembers the last working provider |
| **Video player + download** | Watch saved videos in-browser or download them with one click |
| **Categories** | Tag videos at add-time; filter by category in the grid |

---

## Architecture

```
frontend/   Next.js 15 (App Router) — video grid, download queue UI, player
backend/    FastAPI
  routers/  REST endpoints (auth, videos, queue, download)
  services/
    scraper.py      Playwright pipeline — agentic interact, ffmpeg download, MediaRecorder
    llm_manager.py  Provider abstraction + fallback chain (OpenAI, Anthropic, Ollama)
    queue.py        Background job queue with per-job cancellation
  db.py     SQLite via SQLAlchemy
```

---

## Getting started

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

**2. Backend environment**

Create `backend/.env`:
```env
OPENAI_API_KEY=sk-...          # optional
ANTHROPIC_API_KEY=sk-ant-...   # optional
OLLAMA_HOST=http://127.0.0.1:11434  # optional, defaults to this
LLM_PROVIDER=                  # leave blank for auto-fallback, or set "openai" / "anthropic" / "ollama"
AUTH_PASSWORD=yourpassword     # leave blank to disable auth
CORS_ORIGINS=http://localhost:3000
TEMP_STORAGE_DIR=temp_storage
```

**3. Run**
```bash
./run.sh
```

Open [http://localhost:3000](http://localhost:3000).

---

## Usage

1. Paste a video page URL (any site).
2. Enter a category name.
3. Click **Add Video**.
4. Watch progress in the Downloads panel — the job moves through `extracting → queued → processing → done`.
5. Once done, the video appears in the grid. Click ▶ to play in-browser, or the download icon to save to disk.

---

## License

MIT — see [LICENSE](LICENSE).

---

Created by Mostofa Shakib
