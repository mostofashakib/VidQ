# Trim Video Feature — Design Spec

**Date:** 2026-06-20
**Status:** Approved

---

## Overview

A new Trim page that lets users upload a video, select start and end times using a visual editor (video player + dual-handle range slider + editable time inputs), and submit a trim job whose result is saved to the library.

---

## Architecture

- **Route:** `/trim` — new Next.js page, added to `NAV_LINKS` in `frontend/src/components/Navbar.tsx`
- **Pattern:** Same as combine/translate — router returns `job_id` immediately, daemon thread processes via ffmpeg, frontend polls every 2s
- **Video preview:** Browser `object URL` created from the dropped file; shown in an HTML5 `<video>` element with no server upload until the user clicks Trim
- **localStorage key:** `vidq_trim` — persists non-terminal jobs and restores them on page reload
- **Global semaphore:** Trim worker acquires the same `threading.BoundedSemaphore(5)` as all other workers

---

## Frontend

### File: `frontend/app/trim/page.tsx`

**Phase 1 — Upload**
- Drop zone identical to combine/translate: `max-w-3xl mx-auto`, glass-panel, drag-and-drop or click to browse
- On file drop: create `object URL`, set `phase = "editor"`

**Phase 2 — Editor** (replaces drop zone)

```
┌─────────────────────────────────────────┐
│           HTML5 video player            │
│         (native controls)               │
└─────────────────────────────────────────┘
  [Set Start]                  [Set End]

  ├──●━━━━━━━━━━━━━━━━━━━━━━━●──────────┤

  00:00:04          00:01:32

                  [ Trim ]
```

- **Video player:** `<video>` with `src={objectUrl}` and native controls; `onLoadedMetadata` captures duration
- **Set Start / Set End buttons:** Set the respective handle to `videoRef.current.currentTime`
- **Dual-handle slider:** Two overlapping `<input type="range">` elements styled with CSS; track between handles highlighted
- **Time inputs:** Editable fields in `HH:MM:SS` format, clamped to `[0, duration]`
- **Sync rules:**
  - Slider drag → updates time input
  - Time input edit → updates slider handle
  - Set Start/End button → updates both slider and input
  - Start handle is always ≤ End handle

**Library** (below editor, always visible in phase 2):
- Same polling/display pattern as other features
- Shows queued, processing, done, and failed trim jobs
- Each completed job has a download link

**Page state:**
```ts
type Phase = "upload" | "editor"

// file: File | null
// objectUrl: string | null
// duration: number          (seconds, from video metadata)
// startTime: number         (seconds)
// endTime: number           (seconds)
// phase: Phase
// jobs: TrimJob[]
```

---

## Backend

### File: `backend/app/routers/trim.py`

**`POST /api/trim/start`**
- Accepts `multipart/form-data`: `file` (video), `start_time` (float, seconds), `end_time` (float, seconds)
- Validates `0 ≤ start_time < end_time`
- Saves uploaded file to `temp_storage/{job_id}_input.{ext}`
- Enqueues job to thread pool
- Returns: `{ job_id, status: "queued" }`

**`GET /api/trim/status/{job_id}`**
- Returns: `{ job_id, status, progress, result_url, error }`
- `status` values: `queued | processing | done | failed`

### File: `backend/app/workers/trim_worker.py`

ffmpeg command:
```
ffmpeg -i {input} -ss {start_time} -to {end_time} -c copy -avoid_negative_ts 1 {output}
```

- `-c copy`: stream copy — no re-encode, trims in under a second regardless of video length
- `-avoid_negative_ts 1`: prevents timestamp issues at the cut point
- Trims snap to nearest keyframe (acceptable tradeoff for speed)
- Output: `temp_storage/{job_id}_trimmed.mp4`
- Uses `-progress pipe:1` to report real-time progress
- Acquires global semaphore before starting, releases on completion/failure

### Registration

Router mounted in `backend/app/main.py`:
```python
from app.routers import trim
app.include_router(trim.router, prefix="/api/trim")
```

---

## Data Flow

```
User drops file
  → object URL created → video player shows preview
  → user sets start/end via slider/inputs/buttons
  → clicks Trim
  → POST /api/trim/start (file + start_time + end_time)
  → job_id returned → saved to localStorage vidq_trim
  → poll /api/trim/status/{job_id} every 2s
  → on done: result_url shown in library with download link
  → input temp file cleaned up
```

---

## Out of Scope

- Frame-accurate trim (requires re-encode; keyframe snap is acceptable)
- Multiple trim segments in one pass
- Preview of just the selected range before trimming
- Waveform/thumbnail timeline
