# VidQ

VidQ finds and downloads any video with AI agents, then lets you edit it locally. It searches, downloads, converts, combines, translates, trims and enhances videos from one self-hosted web app.

It's for people who want a private video workstation they clone, run and use without digging through dependency docs.

## Install

You need:

- Python 3.10+
- Node.js 24+ for `agent-browser` (Node 18+ is enough with `SKIP_AGENT_BROWSER=1`)
- macOS or Linux
- At least one LLM provider for the Download, Search and Translate agents
- Google Chrome, if you use the `patchright` browser provider

Then run:

```bash
git clone https://github.com/mostofashakib/VidQ.git
cd VidQ
./setup.sh
```

The setup script installs:

- system tools it knows how to install (`curl`, `unzip`, Node.js and npm)
- `uv`, if it's missing
- backend Python packages from `backend/requirements.txt`
- backend test packages from `backend/requirements-dev.txt`
- Playwright Chromium
- `agent-browser` and its managed Chrome
- Playwright's Linux system libraries, on Linux
- frontend packages from `frontend/package-lock.json`
- the Real-ESRGAN ncnn and Python backends for Enhance
- Real-ESRGAN model weights for the Python backend
- `backend/.env`, copied from `backend/.env.example` if it doesn't exist yet

Set any of these to `1` to change what setup does:

- `SKIP_SYSTEM_DEPS` skips system packages and prints install instructions instead.
- `SKIP_REAL_ESRGAN` skips the ncnn Enhance backend.
- `SKIP_PYTHON_REALESRGAN` skips the Python Real-ESRGAN backend.
- `SKIP_PLAYWRIGHT` skips the Chromium download.
- `SKIP_AGENT_BROWSER` skips `agent-browser`. Downloads then use Playwright's Chromium.
- `SKIP_PLAYWRIGHT_SYSTEM_DEPS` skips Playwright's Linux system packages.
- `FORCE_INSTALL` reinstalls everything setup manages.

For example:

```bash
SKIP_REAL_ESRGAN=1 ./setup.sh
```

## Quick start

```bash
./run.sh
```

Then open http://localhost:3000.

`./run.sh` runs setup, clears temporary output, starts FastAPI on port `8000` and starts Next.js on port `3000`. If nvm is installed, it switches the frontend to Node 22 first. Skip setup on later runs with `SKIP_SETUP=1 ./run.sh`, and stop the app with `Ctrl+C`.

## Configuration

Settings live in `backend/.env`.

A minimal local config:

```env
DATABASE_URL=sqlite:///./videos.db
CORS_ORIGINS=http://localhost:3000
BASE_URL=http://localhost:8000
```

To run the LLMs locally with Ollama:

```env
LLM_PROVIDER=ollama
TRANSLATE_LLM_PROVIDER=ollama
OLLAMA_HOST=http://127.0.0.1:11434
OLLAMA_MODEL=gemma4:26b
```

To use hosted LLMs:

```env
OPENAI_API_KEY=
ANTHROPIC_API_KEY=
OPENROUTER_API_KEY=
OPENROUTER_MODEL=google/gemma-4-31b-it:free
```

Each provider's model is configurable. These are the defaults:

```env
OPENAI_MODEL=gpt-4o
CLAUDE_MODEL=claude-haiku-4-5-20251001
```

To put the app behind a password:

```env
AUTH_ENABLED=true
APP_PASSWORD=change-me
```

### Download browser

```env
BROWSER_PROVIDER=agent-browser
BROWSER_HEADLESS=true
PROXY_URLS=http://user:pass@host:port,socks5://host2:port2
```

`BROWSER_PROVIDER` picks the browser that Download drives:

- `agent-browser` is the default. VidQ attaches to its Chrome session over CDP, so the network interception and recording code works unchanged. If it fails to start, VidQ falls back to Playwright's bundled Chromium.
- `patchright` drives your installed Google Chrome through a driver that hides automation signals. Use it for sites behind interactive Cloudflare checks, which reject the other two. It needs Google Chrome installed, so the Docker image stays on `agent-browser`.
- `playwright` uses the bundled Chromium directly.

All three run headless by default. `BROWSER_HEADLESS=false` opens a visible window for debugging (with `patchright`, the window opens off-screen). Whichever browser runs, VidQ sends its real user agent. A made-up one that doesn't match the engine gets blocked.

`PROXY_URLS` is a comma-separated proxy pool. When Cloudflare challenges a page, VidQ retries through random proxies from the pool, moving on whenever one fails to connect. The last attempt always goes direct.

### Search

```env
SEARCH_SAFE_SEARCH=off
```

`SEARCH_SAFE_SEARCH` sets the safe-search level for the search engines that Search drives in the browser: `off` (the default), `moderate` or `strict`. Search uses the same browser and LLM settings as Download.

### Enhance backend

```env
REAL_ESRGAN_BACKEND=auto
REAL_ESRGAN_BIN=/Users/you/.local/opt/realesrgan-ncnn-vulkan/realesrgan-ncnn-vulkan
REAL_ESRGAN_PYTHON=/path/to/backend/.realesrgan-venv/bin/python
REAL_ESRGAN_MODEL_PATH=/path/to/backend/models/realesrgan/RealESRGAN_x4plus.pth
```

## Features

- Search: describe the video you want in plain words and get the 5 best matches from across the web, with a reason for each. Play any result in the app, send it to Download, or load 5 more.
- Download: paste a link and VidQ finds the video, skips ads and trailers, and saves it. A link to an album page queues every video on it as a separate download.
- Convert: upload a video and get an H.264/AAC 1280×720 MP4. Letterboxing or pillarboxing keeps the aspect ratio.
- Combine: drop in 2 to 20 clips and get one MP4 with crossfades.
- Translate: upload a video and get it back with burned-in English subtitles.
- Trim: upload a video and export the segment you select.
- Enhance: upload low-quality footage and VidQ upscales it with Real-ESRGAN, in parallel chunks.

Every feature has its own job queue with progress, cancel buttons and download links.

## Dependency notes

### Real-ESRGAN

Homebrew doesn't ship `realesrgan-ncnn-vulkan`, so VidQ installs two Enhance backends. The ncnn Vulkan one is fast but crashes on some macOS GPU and Vulkan setups. The Python one runs the upstream PyTorch code. It's slower and takes over when ncnn crashes.

`./setup.sh` installs the official release binary into `~/.local/opt/realesrgan-ncnn-vulkan` and links it into `~/.local/bin/realesrgan-ncnn-vulkan`. If `backend/.env` exists, setup also writes `REAL_ESRGAN_BIN`, `REAL_ESRGAN_PYTHON` and `REAL_ESRGAN_MODEL_PATH` for you.

### Whisper

VidQ transcribes locally with `faster-whisper` by default:

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

Download needs Chromium for browser automation. Setup installs it with:

```bash
uv run playwright install chromium
```

## Commands

```bash
./setup.sh
./run.sh
./kill.sh
backend/.venv/bin/pytest
cd frontend && npm run build
```

## Project layout

```text
frontend/                    Next.js app
  app/                       Pages for Download, Search, Convert, Combine, Translate, Trim, Enhance
  src/components/            Shared UI

backend/                     FastAPI app
  app/routers/               API routes
  app/services/              Workers, browser automation, media pipelines
  tests/                     Backend tests

setup.sh                     One-command setup, including the macOS Real-ESRGAN install
```

## Architecture

VidQ is a local full-stack app:

```text
Browser UI
  ↓
Next.js app routes
  ↓
FastAPI routers
  ↓
Background worker queues
  ↓
Media tools, browser automation, LLM providers and local storage
```

- Frontend: Next.js App Router pages in `frontend/app` handle upload forms, progress polling, cancelling, playback and downloads.
- API: FastAPI routers in `backend/app/routers` validate requests, save uploads, create jobs and report job status.
- Workers: services in `backend/app/services` run the long video tasks outside the request handlers, so the UI stays responsive.
- Queue runtime: shared helpers keep job state, cancellation, cleanup and the global concurrency limit in one place.
- Media: `imageio-ffmpeg`, `yt-dlp`, `agent-browser`, Patchright and Playwright do the downloading, probing, converting, trimming, combining, subtitling and final MP4 output.
- AI: Download uses LLM-guided browser navigation and the ComputerUse engine. Search uses an LLM to plan queries and score results. Translate uses Whisper plus an LLM. Enhance uses Real-ESRGAN.
- Storage: SQLite holds saved video metadata. Generated files live in `backend/temp_storage`, and FastAPI serves them back.

## How it's built

- Convert saves the upload, transcodes it to H.264/AAC 1280×720 MP4 with padding to keep the aspect ratio, and adds the result to the uploaded video library.
- Combine takes the clips in order and runs a single ffmpeg pass to a 720p MP4, padded the same way.
- Translate extracts the audio, transcribes it locally with `faster-whisper` or with OpenAI Whisper, translates the text, builds subtitles and burns them in.
- Trim takes start and end timestamps from the UI, and ffmpeg exports that segment.
- Enhance splits long videos into chunks, upscales them with Real-ESRGAN (ncnn first, Python when ncnn crashes) and stitches video and audio back together.
- Setup installs the Python, Node, browser, frontend, backend and Enhance dependencies, so a fresh clone starts with `./run.sh`.

Download is more involved, so it gets its own section.

## How downloading works

When you submit a link, the backend first fetches the page's plain HTML and checks whether it's an album. If the HTML lists one or more video players with direct files, each one becomes its own job, titled "Album title (2/3)" and using the player's poster as the thumbnail. Each file downloads with the page as `Referer`, because album media hosts refuse requests without it. Players inside ad containers and looping hover previews don't count. A lone video also has to be reachable this way, or the link goes through the regular pipeline. Pages built with JavaScript go through the regular pipeline too.

Each job then tries these stages in order and stops at the first one that works:

1. Direct files and minimal embed pages download straight away with ffmpeg, with curl as a fallback.
2. `yt-dlp` tries the page.
3. The browser opens the page. It gets past Cloudflare if needed (rotating proxies, then clicking through the challenge), asks an LLM where the player controls are, starts playback and collects every media URL the page loads. It then picks the main video (see below) and downloads it with the browser session's cookies. Media files often sit behind the same Cloudflare check as the page.
4. If nothing downloads, the ComputerUse engine drives the player and records it in-page with MediaRecorder.

VidQ keeps a browser profile in `BROWSER_PROFILE_DIR` between sessions, so sites see a returning visitor rather than a fresh browser every time.

### Picking the main video

A page loads lots of video besides the one you want: pre-rolls, floating ad sliders, hover previews in the related grid, trailers. VidQ scores every URL against evidence from the page. A URL is trusted when it matches the JSON-LD `contentUrl`, plays in the largest player outside any ad container, or contains the page's numeric video ID (`/video/90563/...` gives `90563`). URLs inside ad containers (class or id names like `ads`, `sponsor`, `promo`, `banner` or `preroll`) and URLs on known ad networks get rejected outright.

When the page names its video, only trusted URLs get tried. If they're all blocked, VidQ moves on to the next stage instead of grabbing some other clip.

Trailers are harder than ads. They're the same content cut shorter, and they often share the real video's ID and host. VidQ checks three things:

1. Names. A file with `trailer`, `preview`, `teaser`, `sample` or `clip` in its path, label or container is dropped when a file without those words exists.
2. Length. Before downloading anything, ffmpeg reads each file's header (about a second per file) to get its length. A file under half the length the page states is a trailer.
3. Rivals. Files that share the page's video ID are tried longest first.

If every file turns out to be a trailer, the job fails with "Only a trailer is available on this page" instead of saving one. A finished download goes through the same length check. When the page states no length, a short clip (under 60 seconds) from an untrusted URL counts as an ad.

## How search works

Search turns a description into videos you watch or download. A search runs in the background while the page polls for progress, so each step shows as it happens: planning, searching, ranking and checking for duplicates.

1. Planning. Your description is always the first query, exactly as you typed it. The LLM adds up to 4 rewordings that keep every name and term you wrote. It never adds a genre, format or topic you didn't mention, so a search for two people's names doesn't turn into a search for documentaries about them.
2. Searching. Every query runs on every source at once. YouTube and Bilibili go through `yt-dlp`'s built-in search. DuckDuckGo and Brave video results load in a headless browser, and VidQ reads their outbound links, so it doesn't depend on either engine's markup. Before reading a page, VidQ strips scripts, styles, frames, page chrome, hidden elements, ad containers and every attribute except links, titles and image sources. Only results that show a length count, which drops each engine's own promo links. A failing source gets skipped, and the search carries on with the rest.
3. Ranking. Results found by more sources, and results whose titles share more words with your description, go first. The top 20 go to the LLM, which scores every one from 0 to 3. Only scores of 2 or 3 show up. A match needs every name you gave, spelled the same, while synonyms count ("siblings" matches "brother and sister"). Scoring every result keeps a local model from stopping at the first literal match. If the LLM is down, results come back in source order and the page says so.
4. Paging. "More results" serves results already ranked first, and searches the next page of every source only when those run out.

### Duplicates

The same video turns up at several addresses, often under different titles. VidQ treats two results as one video when any of these match:

1. The address, after removing tracking parameters, `www.` and `m.` prefixes, and search-engine redirect wrappers.
2. The video ID. `yt-dlp`'s URL patterns map every form of a link (`watch?v=`, `youtu.be`, `/shorts/`) to one ID without a network call.
3. Length and title. Results on different sites within 3 seconds of each other, whose titles share at least 3 distinctive words and half the words of the shorter title.
4. The picture. Results on different sites within 2 seconds of each other get their frames compared before the second one shows. ffmpeg reads one frame at a quarter, half and three quarters of the way through each video and shrinks it to a 64-bit fingerprint. Two matching frames, with none that clearly differs, mean the same video. Re-encodes, resizing and watermarks change a few bits of a fingerprint, while different footage changes about half.

On a single site, matching titles and lengths count as separate uploads. Every check covers the current page and every result shown before it.

### Playing results

Play opens a player in the app. The backend resolves the result with `yt-dlp`, preferring an H.264 file over an HLS stream so every browser plays it, and checks that the site serves the first bytes. The backend then relays the stream with the headers and cookies the site expects, and passes byte ranges through so seeking works. HLS playlists get their segment links rewritten to point at the backend, and the player loads them with `hls.js`.

When the site refuses the stream, VidQ looks for the page's oEmbed link and shows the site's own embedded player instead. YouTube videos often play this way. When neither works, the player offers a link to open the video on the site.

The relay only fetches the stream it resolved and the parts its playlists list, and every redirect goes through the same safe-URL check as the rest of the app. A `<video>` element has no way to send a login token, so the stream link uses a random ID that expires after an hour.

### Enhance

Enhance runs Real-ESRGAN over 60-second chunks:

1. Split the video into 60-second chunks.
2. Process up to 5 chunks at a time, within the shared worker limit.
3. Extract frames and upscale them with ncnn. Switch to the Python backend if ncnn crashes.
4. Reassemble the chunks in order and mux the original audio back in.

Chunking keeps disk use bounded on long videos, and the shared limit stops one Enhance job from starving the rest of the queue.

## The ComputerUse fallback

A lot of sites fight automated downloads. `yt-dlp`, `curl` and stream sniffers fail on players built as client-side state machines, single-page video apps, encrypted blob streams and Cloudflare Turnstile checks. When VidQ fails to intercept a stream, it has to drive the player the way a person would. That's the hardest part of the project, because sites set traps:

- Overlays. Invisible click-jackers, fake play buttons, cookie walls and age gates sit on top of the video. A naive click opens a popup, an ad redirect or a page reload instead of playing the video.
- Iframes. Players often live in nested cross-origin iframes, out of reach for `document.querySelector`.
- Unstable markup. Some players rename their CSS classes on every deploy. Others draw their controls on a canvas, with no DOM elements at all.
- Popups and navigation. Clicking a player fires `alert()` or `confirm()` dialogs, opens new tabs or navigates away from the page.

### How ComputerUse handles it

Every browser interaction goes through the `ComputerUse` interface in `backend/app/services/scraper/computer_use.py` and `playback.py`. It escalates through these steps until the video plays:

1. Media Session API and a direct `video.play()` call, with no LLM involved, for players with standard controls.
2. The accessibility tree (`aria_snapshot` and `get_by_role`) instead of CSS selectors. A role and name like `button[name~="Play"]` survives renamed classes, and the search covers the main frame and every child frame to dismiss consent dialogs and start playback.
3. A vision LLM. VidQ sends a screenshot, a compact ARIA tree and cleaned-up page HTML, and the model points out the real play button among the decoys.
4. Pixel clicks. For sandboxed iframes and canvas players, `ComputerUse` clicks viewport coordinates with `page.mouse.click(x, y)`. It aims at the largest visible video or iframe, or at coordinates the LLM suggests.

Along the way, it dismisses native dialogs, closes popup tabs as soon as they open, resets when the page navigates away and retries clicks up to 10 times to get through stacked overlays. Once playback is confirmed and the player is fullscreen, VidQ injects `MediaRecorder` into the player's frame and records the stream there, out of reach of network-level blocking.

## License

MIT. See [LICENSE](LICENSE).

Built by [Mostofa Shakib](https://www.mostofashakib.com).
