# Enhance Video Feature — Design Spec

**Date:** 2026-06-20
**Status:** Approved

---

## Overview

A new Enhance page that lets users upload an old or low-quality video and submit it for AI-powered restoration. The backend uses Real-ESRGAN (`realesrgan-x4plus` model) to remove noise, grain, compression artifacts, and blur — the exact degradations present in old camera footage — and outputs clean, sharp video at HD quality (minimum 720p). Processing runs in a background thread pool; the frontend polls for progress and shows the result in a library.

---

## Architecture

- **Route:** `/enhance` — new Next.js page, added to `NAV_LINKS` in `frontend/src/components/Navbar.tsx` after "Trim"
- **Pattern:** Same as combine/trim — router returns `job_id` immediately, daemon thread processes, frontend polls every 3s (longer interval than other jobs; enhancement takes minutes to hours)
- **External tool:** `realesrgan-ncnn-vulkan` binary on server PATH (install: `brew install realesrgan-ncnn-vulkan`). Worker detects missing binary at job start and sets `status = "failed"` with a clear error message.
- **Chunked processing:** Video is split into 60-second segments before frame extraction. At any moment, only one chunk's frames are on disk (~2–4 GB peak instead of 50–100 GB for a 3-hour video).
- **Global semaphore:** Enhance worker acquires the same `threading.BoundedSemaphore(5)` as all other workers.
- **localStorage key:** `vidq_enhance` — persists non-terminal jobs, restores on page reload.

---

## Processing Pipeline

```
Upload file → save to temp_storage/{job_id}_enhance_in.{ext}

Phase "splitting" (progress 1–5%):
  1. ffprobe: get fps, duration, width, height
  2. ffmpeg: extract audio track → temp_storage/{job_id}_audio.aac (if audio stream present)
  3. ffmpeg: split video into 60s segments → temp_storage/{job_id}_chunks/chunk_%04d.mp4

Phase "enhancing N/M" (progress 5–90%, distributed across chunks):
  For each chunk_N.mp4:
    a. mkdir frames_{N}/ enhanced_{N}/
    b. ffmpeg: extract frames as JPEG (qscale:v 2)
         ffmpeg -i chunk_N.mp4 -qscale:v 2 frames_{N}/%08d.jpg
    c. realesrgan-ncnn-vulkan: AI upscale 4x
         realesrgan-ncnn-vulkan -i frames_{N}/ -o enhanced_{N}/
           -n realesrgan-x4plus -s 4 -t 128 -j 1:4:1
    d. ffmpeg: reassemble chunk, scale to target height, encode H.264 CRF 18
         ffmpeg -framerate {fps} -i enhanced_{N}/%08d.jpg
           -vf "scale=-2:{target_height}" -c:v libx264 -crf 18 -preset slow
           -pix_fmt yuv420p enhanced_chunk_{N}.mp4
    e. delete frames_{N}/ and enhanced_{N}/

Phase "assembling" (progress 90–99%):
  4. ffmpeg concat: join all enhanced_chunk_N.mp4 into video-only output
  5. If audio exists: mux audio back with -c:a copy
  6. Output: temp_storage/enhanced_{job_id}.mp4

Cleanup: delete all temp dirs and input file regardless of outcome.
```

**Target height:** `max(720, original_height)` — ensures at least 720p output. If original is ≥ 720p, keeps original height (AI still enhances quality). Width is always `-2` (proportional, rounded to nearest even number by ffmpeg).

**fps probe:** use `ffprobe` (bundled with `imageio_ffmpeg`) with `-show_streams -print_format json` to extract `r_frame_rate` and `duration`.

---

## Backend

### File: `backend/app/services/enhance_worker.py`

Thread pool (MAX_WORKERS = 5), global semaphore, same pattern as `trim_worker.py`.

```python
class EnhanceJob:
    def __init__(self, job_id: str, input_path: str):
        self.job_id = job_id
        self.input_path = input_path
        self.status = "queued"      # queued | processing | done | failed | cancelled
        self.phase = "queued"       # queued | splitting | enhancing N/M | assembling
        self.progress: int = 0
        self.error: Optional[str] = None
        self.result_url: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None
```

Public API:
```python
def start_enhance_job(input_path: str) -> str: ...   # returns job_id
def get_job(job_id: str) -> Optional[EnhanceJob]: ...
def cancel_job(job_id: str) -> bool: ...
```

**Binary detection** (at start of `_process_job`):
```python
import shutil
if not shutil.which("realesrgan-ncnn-vulkan"):
    job.status = "failed"
    job.error = "realesrgan-ncnn-vulkan not found. Install with: brew install realesrgan-ncnn-vulkan"
    return
```

**Cancel:** sets `job.status = "cancelled"`, calls `job._proc.kill()`. The main processing loop checks `job.status == "cancelled"` after each subprocess and exits early, cleaning up all temp dirs for that job.

**Temp directory layout** for job `{job_id}`:
```
temp_storage/
  {job_id}_enhance_in.{ext}     # uploaded input (deleted in finally)
  {job_id}_audio.aac             # extracted audio (deleted in finally)
  {job_id}_chunks/               # 60s video segments (deleted in finally)
    chunk_0000.mp4
    chunk_0001.mp4
    ...
  {job_id}_frames_{N}/           # extracted frames for chunk N (deleted after chunk)
  {job_id}_enhanced_{N}/         # AI-processed frames for chunk N (deleted after chunk)
  enhanced_{job_id}.mp4          # final output (kept, served via /temp_storage/)
```

**Progress calculation:**
- splitting: `progress = 3`
- enhancing: `progress = 5 + int((chunk_idx / total_chunks) * 85)`
- assembling: `progress = 92`
- done: `progress = 100`

### File: `backend/app/routers/enhance.py`

```python
class EnhanceJobOut(BaseModel):
    job_id: str
    status: str
    phase: str = "queued"
    progress: int = 0
    result_url: Optional[str] = None
    error: Optional[str] = None

@router.post("/enhance-video", response_model=EnhanceJobOut)
async def start_enhance(
    file: UploadFile = File(...),
    token: str = Depends(verify_token),
): ...
# Saves file to temp_storage, calls start_enhance_job, returns EnhanceJobOut

@router.get("/enhance-jobs/{job_id}", response_model=EnhanceJobOut)
def get_enhance_status(job_id: str, token: str = Depends(verify_token)): ...
# 404 if not found

@router.delete("/enhance-jobs/{job_id}", status_code=204)
def cancel_enhance_job(job_id: str, token: str = Depends(verify_token)): ...
# 404 if not found; no-op if already terminal
```

### File: `backend/app/main.py`

Add:
```python
from app.routers.enhance import router as enhance_router
app.include_router(enhance_router)
```

---

## Frontend

### File: `frontend/app/api.ts`

Append:
```typescript
export interface EnhanceJobData {
  job_id: string;
  status: string;
  phase: string;
  progress: number;
  result_url?: string;
  error?: string;
}

export async function startEnhanceJob(
  token: string,
  file: File,
  onUploadProgress?: (percent: number) => void,
): Promise<EnhanceJobData>
// POST /enhance-video with FormData: file

export async function getEnhanceJob(token: string, jobId: string): Promise<EnhanceJobData>
// GET /enhance-jobs/${jobId}

export async function cancelEnhanceJob(token: string, jobId: string): Promise<void>
// DELETE /enhance-jobs/${jobId}
```

### File: `frontend/app/jobs-context.tsx`

Add alongside TrimJobItem:
```typescript
export interface EnhanceJobItem {
  localId: string;
  filename: string;
  uploadProgress: number;
  jobId: string;
  data: EnhanceJobData;
}
```

Add to `JobsContextValue`: `enhanceJobs`, `setEnhanceJobs`, `enhancePollRefs`

localStorage restore/persist with key `vidq_enhance`, same pattern as `vidq_trim`.

### File: `frontend/app/enhance/page.tsx`

Single-phase page (no editor step):

**Phase: upload**
- Drop zone (`max-w-3xl mx-auto`, glass-panel, drag-and-drop or click to browse) — identical layout to other pages
- File selected: shows filename, enables "Enhance Video" button
- Click Enhance: `startEnhanceJob(token, file, setUploadProgress)` → adds to `enhanceJobs` → starts polling

**Library** (always visible once jobs exist):
- Poll interval: 3000ms (jobs run for hours — no need to hammer)
- Status display strings:
  - `queued` → "Queued…"
  - `processing` / phase `splitting` → "Splitting video…"
  - `processing` / phase `enhancing N/M` → "Enhancing chunk N of M"
  - `processing` / phase `assembling` → "Assembling final video…"
  - `done` → progress bar full + Download button
  - `failed` → error message in red
- Progress bar: `data.progress` (0–100)
- Recovery polling on mount: re-attach intervals for non-terminal jobs missing from `pollRefs`, same pattern as trim page
- localStorage: save non-terminal jobs on state change, restore on init

**Upload progress:** show a secondary progress bar during file upload (before job starts) using `onUploadProgress`.

### File: `frontend/src/components/Navbar.tsx`

Add `{ href: "/enhance", label: "Enhance" }` after the Trim entry in `NAV_LINKS`.

---

## Data Flow

```
User drops file
  → file selected, Enhance button enabled
  → click Enhance
  → POST /enhance-video (multipart: file)
  → job_id returned → saved to localStorage vidq_enhance
  → poll /enhance-jobs/{job_id} every 3s
  → progress bar and phase label update in library
  → on done: result_url shown with Download button
  → temp input + intermediate files cleaned up by worker
```

---

## Out of Scope

- Model selection (only `realesrgan-x4plus` — best for realistic live-action footage)
- Configurable output resolution (720p minimum is fixed; original height preserved above 720p)
- Frame interpolation / FPS boost
- Color grading or contrast adjustment
- Batch enhancement of multiple files in one job
