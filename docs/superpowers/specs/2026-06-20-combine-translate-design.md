# VidQ: Combine & Translate Features + Shared Navbar

**Date:** 2026-06-20  
**Status:** Approved

---

## Overview

Add two new video-processing routes to VidQ plus a shared navigation bar:

1. **`/combine`** — Drag-and-drop multiple video files, merge them with xfade crossfade transitions, output a downloadable 720p MP4.
2. **`/translate`** — Drag-and-drop a single video, generate timestamped English subtitles via Whisper, translate via local Ollama LLM (fallback: OpenAI), burn subtitles YouTube-style into the video.
3. **Navbar** — Shared sticky nav component replaces duplicated headers on all pages.

All new features follow existing worker/polling patterns from `upload_worker.py` and `upload.py`.

---

## Architecture

### New files

```
backend/app/routers/combine.py
backend/app/routers/translate.py
backend/app/services/combine_worker.py
backend/app/services/translate_worker.py

frontend/src/components/Navbar.tsx
frontend/app/combine/page.tsx
frontend/app/translate/page.tsx
```

### Modified files

```
backend/app/main.py              — register combine_router, translate_router
backend/app/services/llm_manager.py — add call_translate_text() + execute_translate()
frontend/app/api.ts              — add combine + translate API functions
frontend/app/page.tsx            — replace inline header with <Navbar />
frontend/app/upload/page.tsx     — replace inline header with <Navbar />
```

---

## Section 1 — Combine

### Backend: `combine_worker.py`

Mirrors `upload_worker.py` exactly in structure:

- **`CombineJob`** dataclass: `job_id`, `filenames: list[str]`, `status`, `error`, `phase`, `overall_progress: int`, `clip_index: int`, `total_clips: int`, `result_url`, `_proc`
- **`start_combine_job(file_paths, original_names) -> str`** — enqueue, return `job_id`
- **`_worker_loop()`** — same daemon-thread pool pattern (`MAX_WORKERS = 5`)
- **`_process_job()`** — two-phase pipeline:

**Phase 1 — Normalize (reuse `video_utils.ensure_min_quality`):**
```
for i, clip in enumerate(clips):
    normalized = ensure_min_quality(clip)  # scales to 720p
    update job: phase="normalizing", clip_index=i+1
```

**Phase 2 — Concat with xfade:**
Probe each normalized clip's duration with `_probe_duration()` (reuse from `upload_worker`).

Build ffmpeg `filter_complex` with xfade (0.5s fade) between each pair:
```
xfade offset = sum(durations[0..i]) - i * 0.5
acrossfade for audio
```

Run ffmpeg with `-progress pipe:1` and parse `out_time=` for overall progress.

Output: `combined_{job_id}.mp4` in `temp_storage/`.

**Result is NOT added to DB** — user downloads directly via `result_url`.

### Backend: `combine.py` router

```
POST /combine-video        — accepts multipart files[], saves each, enqueues CombineJob
GET  /combine-jobs/{id}    — polling endpoint
DELETE /combine-jobs/{id}  — cancel
```

**Polling response:**
```json
{
  "job_id": "...",
  "status": "queued|processing|done|failed",
  "phase": "normalizing|concatenating",
  "overall_progress": 0-100,
  "clip_index": 2,
  "total_clips": 4,
  "result_url": "/temp_storage/combined_abc123.mp4",
  "error": null
}
```

### Frontend: `/combine/page.tsx`

- Multi-file drag-and-drop zone (accept `video/*`)
- Ordered list of selected files (drag-to-reorder not required for v1 — just list order)
- "Combine" button → `POST /combine-video` with all files as FormData
- Progress bar with phase label:
  - `"Normalizing clip 2/4…"`
  - `"Merging with crossfade… 67%"`
- On done: "Download Combined Video" button linking to `result_url`
- Polls `GET /combine-jobs/{id}` every 2 seconds

### API additions (`api.ts`)

```typescript
startCombineJob(token, files, onUploadProgress?) → { job_id }
getCombineJob(token, jobId) → CombineJobData
cancelCombineJob(token, jobId) → void
```

---

## Section 2 — Translate

### Backend: `translate_worker.py`

**`TranslateJob`** dataclass: `job_id`, `filename`, `status`, `error`, `phase`, `overall_progress: int`, `chunk_index: int`, `total_chunks: int`, `result_url`, `_proc`

**4-phase pipeline:**

**Phase 1 — Extract audio (ffmpeg, ~2s):**
```
ffmpeg -i video.mp4 -vn -ar 16000 -ac 1 audio_{job_id}.wav
```
Fast, no progress needed. `overall_progress = 5`

**Phase 2 — Whisper transcription:**
```python
openai.audio.transcriptions.create(
    file=open(audio_path, "rb"),
    model="whisper-1",
    response_format="verbose_json"
)
# Returns: segments[{start, end, text}]
```
`overall_progress = 30` on completion.

**Phase 3 — LLM translation (chunked):**

Token estimation: `len(text) // 4` per segment.  
Chunk boundary: accumulate segments up to 2000-token budget, then start new chunk.

Translation prompt (per chunk):
```
You are a professional subtitle translator. Translate each subtitle segment below to English exactly as spoken. 
Do NOT summarize, condense, or omit any segment. 
Translate every line individually and return ONLY the translated text in the same format.
Preserve all segment numbering and timing markers exactly.

[SRT SEGMENTS]
1
00:00:01,000 --> 00:00:03,500
<original text>
...
```

Uses `FallbackLLMManager.execute_translate(prompt) -> str` (Ollama first, OpenAI fallback).

Re-assembles chunks into a single SRT file: `subs_{job_id}.srt`.  
Progress: `overall_progress` goes from 30 → 80 linearly across chunks.

**Phase 4 — Burn subtitles (ffmpeg):**
```
ffmpeg -i video.mp4 -vf "scale=-2:720:flags=lanczos,
  subtitles=subs.srt:force_style='Alignment=2,MarginV=30,
  FontName=Arial,FontSize=20,PrimaryColour=&Hffffff,
  OutlineColour=&H000000,Outline=2,Shadow=1'"
  -c:v libx264 -crf 18 -preset slow -c:a copy
  -progress pipe:1 -nostats
  translated_{job_id}.mp4
```
`out_time=` parsing → `overall_progress` 80→100.

Cleans up `audio_*.wav` and `subs_*.srt` on completion.

**Result NOT added to DB** — downloadable via `result_url`.

### LLM manager additions (`llm_manager.py`)

Add `call_translate_text(prompt: str) -> str` to each provider (returns raw text, no JSON parsing).  
Add `execute_translate(prompt: str) -> str` to `FallbackLLMManager`.  
Does NOT change any existing `call_text` / `call_vision` / `execute` / `execute_text` methods.

**Ollama translate** — omit `"format": "json"` so the model returns plain text.

### Backend: `translate.py` router

```
POST /translate-video       — accepts single file, enqueues TranslateJob
GET  /translate-jobs/{id}   — polling
DELETE /translate-jobs/{id} — cancel
```

**Polling response:**
```json
{
  "job_id": "...",
  "status": "queued|processing|done|failed",
  "phase": "extracting_audio|transcribing|translating|burning",
  "overall_progress": 0-100,
  "chunk_index": 3,
  "total_chunks": 7,
  "result_url": "/temp_storage/translated_abc123.mp4",
  "error": null
}
```

### Frontend: `/translate/page.tsx`

- Single-file drag-and-drop zone (accept `video/*`)
- "Translate" button → `POST /translate-video`
- 4-phase progress bar with label:
  - `"Extracting audio…"`
  - `"Transcribing with Whisper…"`
  - `"Translating (chunk 3/7)…"`
  - `"Burning subtitles… 80%"`
- On done: "Download Subtitled Video" button
- Polls every 2 seconds

### API additions (`api.ts`)

```typescript
startTranslateJob(token, file, onUploadProgress?) → { job_id }
getTranslateJob(token, jobId) → TranslateJobData
cancelTranslateJob(token, jobId) → void
```

---

## Section 3 — Shared Navbar

`frontend/src/components/Navbar.tsx` — replaces the inline header on all 4 pages.

```tsx
// Sticky glass-panel header, matching existing visual style
// Links: Library (/), Upload (/upload), Combine (/combine), Translate (/translate)
// Active route: indigo underline via usePathname()
// Right side: Logout button (same behavior as existing logout)
```

Replace the header JSX in `page.tsx` and `upload/page.tsx` with `<Navbar />`.  
New pages `combine/page.tsx` and `translate/page.tsx` use `<Navbar />` from the start.

---

## Code Reuse

| Reused from existing code | Where |
|---|---|
| `_probe_duration()` from `upload_worker.py` | Copied into `combine_worker.py` (no shared util currently) |
| `ensure_min_quality()` from `video_utils.py` | Called directly in `combine_worker.py` normalize phase |
| `probe_video_dimensions()` from `video_utils.py` | Called for combine xfade offset calculation |
| `_scale_to_720p()` progress-parsing pattern | Replicated in combine concat phase for `out_time=` |
| `start_upload_job` / `get_job` / `cancel_job` pattern | `combine_worker.py` and `translate_worker.py` follow exact same API shape |
| `UploadJobOut` Pydantic shape | `CombineJobOut`, `TranslateJobOut` follow same pattern |
| Auth: `verify_token` dependency | All new endpoints |
| `FallbackLLMManager` | `translate_worker.py` calls `execute_translate()` |
| Worker pool (`_ensure_pool`, daemon threads) | Both new workers |

---

## Error Handling

- ffmpeg failure: set `status="failed"`, `error=<last 200 chars of stderr>`, clean temp files
- Whisper failure: set `status="failed"`, `error=<message>`
- LLM failure: all providers exhausted → `status="failed"`, `error="Translation failed: <msg>"`
- Cancel: kill `_proc` if running (same as `upload_worker.cancel_job`)

---

## Non-Goals

- Combined/translated videos are NOT added to the DB/Library — they are ephemeral download-only outputs
- No drag-to-reorder UI for clip ordering (file input order is used)
- No subtitle language selection (English output only)
- No SRT download separate from burned video
