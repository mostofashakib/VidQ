# VidQ

**Paste a URL. VidQ downloads the video.** No browser extensions, no site-specific configs — it works by navigating the page like a human, handling cookie banners, ad overlays, and custom video players automatically. Works on standard video sites as well as streaming platforms that embed players inside iframes and serve HLS streams with token-based URLs.

---

## How It Works

VidQ runs a multi-stage pipeline inside a headless Chromium browser:

### Stage 0 — Direct Embed Fast-Path
If the URL points directly to a video file or a minimal HTML wrapper around one, VidQ downloads it immediately via `curl`/`yt-dlp` — no Playwright or LLM invocation needed.

### Stage 1 — Fast Pass (Network Sniff + Agentic Interact)
1. Opens the page and intercepts both **requests** (by file extension in the URL path) and **responses** (by `Content-Type` header). This catches HLS manifests like `playlist.m3u8?token=abc...` whose full URL doesn't end in `.m3u8`.
2. Uses persisted browser state (`storage_state.json`, ignored by git) so repeat visits keep cookies, localStorage, and consent state.
3. Detects whether the video **auto-started** (no click needed) and skips the interaction loop if so.
4. If playback hasn't started, runs a layered **agentic interaction loop** to start it:
   - **MediaSession pre-kick** — tries `video.play()` and fullscreen through browser media APIs.
   - **Accessibility + JS heuristics** — dismisses cookie banners, skip-ad buttons, countdowns, age-gates, and play overlays.
   - **LLM-guided selector/pixel click** — uses screenshot + ARIA tree + cleaned HTML when heuristics are not enough.
   - **Popup retry handling** — re-clicks play targets up to 10 times when ad popups or overlays interrupt playback.
   - **Strategy cache** — once a strategy starts playback, later retries replay that strategy first instead of walking the whole stack.
   - **Refresh recovery** — if fullscreen/play actions refresh the page after media starts, VidQ retries playback setup within the same attempt.
5. Identifies the **main video** (largest on-screen area) across the main frame and all child iframes, then downloads it via `ffmpeg`. Falls back to **yt-dlp** for tokenized HLS/DASH streams that `ffmpeg` can't reassemble.

### Stage 2 — Heavy Pass (MediaRecorder Fallback)
If Stage 1 can't produce a downloadable file (DRM-adjacent content, blob URLs, or encrypted segments), VidQ:
1. Reloads the page in a fresh context.
2. Locates which frame (main or iframe) actually holds the `<video>` element.
3. Injects a `MediaRecorder` into that frame's execution context — this is required because `captureStream()` must run inside the same frame as the video, not the parent page.
4. Confirms playback before injecting the recorder, then records in real time up to the detected video duration when available (capped at 3 hours).
5. Detects looping players and stops once recorded time reaches the video duration instead of recording forever.
6. Detects stuck/static captures by checking frame changes every 30 seconds.
7. Converts the WebM capture to MP4 automatically. If the recording is blank, static, or unavailable, the job fails and the frontend shows the failure instead of saving a bad video.

Videos are stored locally — no expiring CDN links.

---

## Features

### Agentic Navigation
- Works on any website without per-site configuration
- Auto-play detection skips the interaction loop when the video starts immediately
- MediaSession, accessibility, CSS, direct-video, LLM selector, LLM pixel, and heuristic pixel strategies are all supported
- LLM vision model guides click decisions when JS heuristics aren't enough, but metadata extraction is best-effort so a completed recording can still be saved if all LLM providers fail
- Interaction loop searches the main frame **and all child iframes** for play buttons and video elements
- Re-clicks play buttons through popup/ad interruptions and logs each action for debugging
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
- Browser-recorded WebM files without duration metadata are accepted before conversion; final MP4 validation remains strict

### File Upload
- Upload local video files directly — processed through the same quality pipeline
- `.webm` uploads are converted to MP4 automatically
- Per-upload job tracking with progress and cancellation support
- Conversion/scaling failures surface as frontend errors

### Combine Videos
- Drag and drop 2–20 video files on the **Combine** page to merge them into a single video
- Clips are joined with smooth **crossfade transitions** (ffmpeg `xfade` filter, 0.5 s overlap)
- Each clip is normalized to 720p before merging; the final output is a 720p MP4
- Real-time progress bar showing normalize phase (clip N/N) and merge phase (%)
- Output is immediately downloadable — not saved to the library

### Translate & Subtitle Videos
- Drop a video on the **Translate** page to get an English-subtitled version
- **Whisper** (`whisper-1`) generates a timestamped transcript; chunks too large for a single LLM call are split and processed in parallel
- A local **Ollama** LLM (default: `qwen3.5:latest`) translates each subtitle segment; falls back to OpenAI if Ollama is unavailable
- The translation prompt explicitly instructs the model to translate every segment individually — no summarizing or condensing
- Subtitles are burned into the video YouTube-style (bottom-center, white text with black outline) via ffmpeg
- Four-phase progress bar: **Audio → Whisper → LLM (chunk N/N) → Burn**
- Output is immediately downloadable — not saved to the library

### Download Jobs
- Up to **5 URL downloads run at the same time**. Each active download gets its own isolated background thread and Playwright browser/context as needed.
- Downloads beyond the cap stay queued until an active job finishes, then start automatically.
- Active threads and browser instances are torn down after each job completes, fails, or is cancelled.
- Live status updates: `queued → fast_pass → heavy_pass_waiting → heavy_pass_recording → done` or `failed`
- Fast-pass ffmpeg downloads show real progress when duration is known.
- Heavy-pass recording progress uses detected video duration when available instead of always showing the 3-hour cap.
- Completed videos are inserted into the frontend grid immediately from the queue result, without waiting for a full list reload.
- **Cancel any job at any stage** — queued jobs are removed immediately; in-progress jobs receive a cancellation signal and stop at the next cancellation check.

### Video Library
- Saved videos stream directly from local storage — watch in-browser or download with one click
- Tag videos with a category at add-time and filter by category in the grid
- Infinite scroll with deduplication (same URL or title won't be saved twice)
- Remote thumbnails that are not configured for Next Image are skipped instead of breaking the UI
- Uploads have a separate library view and can be downloaded or deleted independently

### Security
- URL validation blocks SSRF attacks (private IPs, loopback addresses, non-HTTP schemes)
- Optional password authentication with timing-safe token comparison
- Auth can be disabled entirely for local-only use

---

## Architecture

```
frontend/   Next.js 15 (App Router) — video grid, download queue panel, in-browser player
  src/components/
    Navbar.tsx        Shared sticky nav (Library, Upload, Combine, Translate)
  app/
    combine/          Combine page — multi-file drag-and-drop → xfade merge
    translate/        Translate page — single file drop → Whisper + LLM → burned subtitles
backend/    FastAPI
  routers/
    auth.py           Password login, Bearer token validation
    video.py          Add video, list, delete, queue status, file serving
    upload.py         Direct file upload with per-job progress tracking
    combine.py        Multi-clip combine endpoint with polling
    translate.py      Video translate endpoint with polling
  services/
    scraper/
      pipeline.py     Three-stage pipeline: embed fast-path → fast pass → MediaRecorder fallback
      playback.py     Agentic interact loop, JS heuristics, force-play, frame-aware helpers
      media.py        ffmpeg/yt-dlp download, quality scaling, ad URL filtering
      html.py         HTML cleaning for LLM context
    llm_manager.py    Provider abstraction + fallback chain (OpenAI → Anthropic → Ollama); includes plain-text translate call
    queue.py          5-concurrent download runner with per-job threads, cancellation, phase tracking, and overflow queuing
    upload_worker.py  Upload processing workers with 720p scaling and WebM-to-MP4 conversion
    combine_worker.py xfade clip merging with per-clip normalize phase
    translate_worker.py Whisper transcription → chunked LLM translation → ffmpeg subtitle burn
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
BROWSER_HEADLESS=true                    # optional
BROWSER_PROFILE_DIR=browser_profile      # optional; stores storage_state.json
PROXY_URLS=                              # optional comma-separated proxy list for Cloudflare fallback
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
5. Once done, the video appears in the grid immediately — click ▶ to play in-browser, or the download icon to save to disk.
6. If playback cannot be confirmed or the MediaRecorder capture is blank/static, the job fails and the frontend shows the error instead of saving a bad file.

To add a local file instead, use the **Upload** link in the nav. Uploaded WebM files are converted to MP4 automatically.

To merge multiple clips, use the **Combine** link in the nav. To generate English subtitles for a video, use the **Translate** link.

---

## License

MIT — see [LICENSE](LICENSE).

---

<div align="center">

Built by [Variant Labs](https://www.vriantlabs.com) · [hello@vriantlabs.com](mailto:hello@vriantlabs.com)

</div>
