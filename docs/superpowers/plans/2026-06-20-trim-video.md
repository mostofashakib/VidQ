# Trim Video Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a Trim page where users upload a video, set start/end points via a dual-handle slider + time inputs + video preview, and submit a background trim job whose result appears in a library.

**Architecture:** Two-phase single page (upload drop zone → editor with video player + controls). Backend follows the existing combine/translate worker pattern: router saves file, enqueues job, returns job_id immediately; daemon thread runs ffmpeg stream-copy trim; frontend polls every 2s. No re-encode — `-c copy` trims in under a second.

**Tech Stack:** Next.js (App Router, TypeScript), FastAPI, ffmpeg via imageio_ffmpeg, axios, lucide-react, Tailwind CSS

## Global Constraints

- Python workers live in `backend/app/services/`
- Routers live in `backend/app/routers/`
- Frontend pages live in `frontend/app/<route>/page.tsx`
- All router paths follow existing pattern: no `/api/` prefix (e.g. `/trim-video`, `/trim-jobs/{job_id}`)
- Job types added to `frontend/app/jobs-context.tsx` and `frontend/app/api.ts`
- localStorage key: `vidq_trim`
- Global semaphore: `from app.services.global_semaphore import global_job_semaphore`
- Temp storage: `settings.temp_storage_dir` (already mounted as `/temp_storage` static files)
- Result URL pattern: `f"{settings.base_url}/temp_storage/{out_filename}"`

---

## File Structure

| File | Action | Purpose |
|------|--------|---------|
| `backend/app/services/trim_worker.py` | Create | Thread pool, TrimJob class, ffmpeg execution |
| `backend/app/routers/trim.py` | Create | FastAPI router: start, status, cancel endpoints |
| `backend/app/main.py` | Modify | Register trim router |
| `frontend/app/api.ts` | Modify | TrimJobData interface + startTrimJob / getTrimJob / cancelTrimJob |
| `frontend/app/jobs-context.tsx` | Modify | TrimJobItem type, state, localStorage restore/persist |
| `frontend/app/trim/page.tsx` | Create | Two-phase trim UI: drop zone → editor + job library |
| `frontend/src/components/Navbar.tsx` | Modify | Add Trim nav link |

---

### Task 1: Backend trim worker

**Files:**
- Create: `backend/app/services/trim_worker.py`

**Interfaces:**
- Produces: `start_trim_job(input_path: str, start_time: float, end_time: float) -> str`, `get_job(job_id: str) -> Optional[TrimJob]`, `cancel_job(job_id: str) -> bool`

- [ ] **Step 1: Create the worker file**

```python
# backend/app/services/trim_worker.py
import os
import queue
import subprocess
import threading
import logging
import uuid
from typing import Optional

import imageio_ffmpeg

from app.config import get_settings

logger = logging.getLogger("TrimWorker")

MAX_WORKERS = 5

_jobs: dict[str, "TrimJob"] = {}
_lock = threading.Lock()
_task_queue: queue.Queue = queue.Queue()

_pool_started = False
_pool_lock = threading.Lock()


class TrimJob:
    def __init__(self, job_id: str, input_path: str, start_time: float, end_time: float):
        self.job_id = job_id
        self.input_path = input_path
        self.start_time = start_time
        self.end_time = end_time
        self.status = "queued"  # queued | processing | done | failed | cancelled
        self.error: Optional[str] = None
        self.progress: int = 0
        self.result_url: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None


def _ensure_pool() -> None:
    global _pool_started
    with _pool_lock:
        if _pool_started:
            return
        for i in range(MAX_WORKERS):
            t = threading.Thread(target=_worker_loop, name=f"trim-worker-{i}", daemon=True)
            t.start()
        _pool_started = True
        logger.info(f"Trim worker pool started ({MAX_WORKERS} workers)")


def _worker_loop() -> None:
    from app.services.global_semaphore import global_job_semaphore
    while True:
        job_id = _task_queue.get()
        try:
            job = _jobs.get(job_id)
            if not job or job.status == "cancelled":
                if job:
                    try:
                        os.remove(job.input_path)
                    except OSError:
                        pass
                logger.info(f"[{job_id}] Skipped (cancelled before pickup)")
                continue
            global_job_semaphore.acquire()
            try:
                with _lock:
                    job.status = "processing"
                logger.info(f"[{job_id}] Worker picked up trim job")
                _process_job(job_id)
            finally:
                global_job_semaphore.release()
        except Exception as e:
            logger.error(f"Trim worker loop error for {job_id}: {e}", exc_info=True)
        finally:
            _task_queue.task_done()


def get_job(job_id: str) -> Optional[TrimJob]:
    with _lock:
        return _jobs.get(job_id)


def cancel_job(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if not job or job.status in ("done", "failed", "cancelled"):
            return False
        job.status = "cancelled"
        if job._proc:
            try:
                job._proc.kill()
            except Exception:
                pass
    return True


def start_trim_job(input_path: str, start_time: float, end_time: float) -> str:
    _ensure_pool()
    job_id = uuid.uuid4().hex
    job = TrimJob(job_id=job_id, input_path=input_path, start_time=start_time, end_time=end_time)
    with _lock:
        _jobs[job_id] = job
    _task_queue.put(job_id)
    logger.info(f"[{job_id}] Queued trim: {start_time:.2f}s – {end_time:.2f}s")
    return job_id


def _process_job(job_id: str) -> None:
    job = _jobs[job_id]
    settings = get_settings()
    out_filename = f"trimmed_{job_id}.mp4"
    out_path = os.path.join(settings.temp_storage_dir, out_filename)
    duration = job.end_time - job.start_time

    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        # -ss before -i: fast keyframe seek. -to is then relative to start.
        cmd = [
            ffmpeg_exe, "-y",
            "-ss", str(job.start_time),
            "-i", job.input_path,
            "-to", str(duration),
            "-c", "copy",
            "-avoid_negative_ts", "1",
            "-progress", "pipe:1", "-nostats",
            out_path,
        ]

        logger.info(f"[{job_id}] Running ffmpeg trim")
        stderr_lines: list[str] = []

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        with _lock:
            job._proc = proc

        def _drain_stderr() -> None:
            try:
                for line in proc.stderr:
                    stderr_lines.append(line)
            except Exception:
                pass

        stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
        stderr_thread.start()

        try:
            for line in proc.stdout:
                line = line.strip()
                if line.startswith("out_time=") and duration > 0:
                    time_str = line[len("out_time="):]
                    try:
                        parts = time_str.split(":")
                        current_s = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                        pct = min(99, int(current_s / duration * 100))
                        with _lock:
                            job.progress = pct
                    except Exception:
                        pass
        except Exception:
            pass

        proc.wait()
        stderr_thread.join(timeout=2)

        with _lock:
            job._proc = None
            cancelled = job.status == "cancelled"

        if cancelled:
            try:
                if os.path.exists(out_path):
                    os.remove(out_path)
            except OSError:
                pass
            return

        if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
            stderr_text = "".join(stderr_lines)
            logger.error(f"[{job_id}] ffmpeg failed: {stderr_text[-400:]}")
            with _lock:
                job.status = "failed"
                job.error = "Trim failed"
            return

        result_url = f"{settings.base_url}/temp_storage/{out_filename}"
        with _lock:
            job.progress = 100
            job.status = "done"
            job.result_url = result_url
        logger.info(f"[{job_id}] Done: {out_filename}")

    except Exception as e:
        logger.error(f"[{job_id}] Process error: {e}", exc_info=True)
        with _lock:
            if job.status == "processing":
                job.status = "failed"
                job.error = str(e)
    finally:
        try:
            if os.path.exists(job.input_path):
                os.remove(job.input_path)
        except OSError:
            pass
```

- [ ] **Step 2: Verify the file exists and imports cleanly**

Run from `backend/`:
```bash
python -c "from app.services.trim_worker import start_trim_job, get_job, cancel_job; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/trim_worker.py
git commit -m "feat: add trim worker with ffmpeg stream-copy"
```

---

### Task 2: Backend trim router + main.py registration

**Files:**
- Create: `backend/app/routers/trim.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Consumes: `start_trim_job`, `get_job`, `cancel_job` from `app.services.trim_worker`
- Produces: `POST /trim-video`, `GET /trim-jobs/{job_id}`, `DELETE /trim-jobs/{job_id}`

- [ ] **Step 1: Create the router**

```python
# backend/app/routers/trim.py
import os
import shutil
import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.config import get_settings
from app.routers.auth import verify_token
from app.services.trim_worker import cancel_job, get_job, start_trim_job

logger = logging.getLogger("TrimRouter")

router = APIRouter()


class TrimJobOut(BaseModel):
    job_id: str
    status: str
    progress: int = 0
    result_url: Optional[str] = None
    error: Optional[str] = None


@router.post("/trim-video", response_model=TrimJobOut)
async def start_trim(
    file: UploadFile = File(...),
    start_time: float = Form(...),
    end_time: float = Form(...),
    token: str = Depends(verify_token),
):
    if start_time < 0 or end_time <= start_time:
        raise HTTPException(status_code=400, detail="start_time must be >= 0 and < end_time")

    settings = get_settings()
    os.makedirs(settings.temp_storage_dir, exist_ok=True)

    ext = os.path.splitext(file.filename or "video.mp4")[1] or ".mp4"
    filename = f"trim_in_{uuid.uuid4().hex}{ext}"
    file_path = os.path.join(settings.temp_storage_dir, filename)

    try:
        with open(file_path, "wb") as f:
            shutil.copyfileobj(file.file, f)
    except Exception as e:
        try:
            os.remove(file_path)
        except OSError:
            pass
        raise HTTPException(status_code=500, detail=f"File upload failed: {e}")

    job_id = start_trim_job(file_path, start_time, end_time)
    logger.info(f"Trim queued: job_id={job_id} start={start_time} end={end_time}")
    return TrimJobOut(job_id=job_id, status="queued")


@router.get("/trim-jobs/{job_id}", response_model=TrimJobOut)
def get_trim_status(job_id: str, token: str = Depends(verify_token)):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return TrimJobOut(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        result_url=job.result_url,
        error=job.error,
    )


@router.delete("/trim-jobs/{job_id}", status_code=204)
def cancel_trim_job(job_id: str, token: str = Depends(verify_token)):
    ok = cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or not cancellable")
    return None
```

- [ ] **Step 2: Register router in main.py**

Open `backend/app/main.py` and add these two lines in the existing import + include block:

```python
# Add this import alongside the other router imports (line ~9):
from app.routers.trim import router as trim_router

# Add this after app.include_router(translate_router) (line ~38):
app.include_router(trim_router)
```

The full imports section becomes:
```python
from app.routers.auth import router as auth_router
from app.routers.video import router as video_router
from app.routers.upload import router as upload_router
from app.routers.combine import router as combine_router
from app.routers.translate import router as translate_router
from app.routers.trim import router as trim_router
```

And the router registration block becomes:
```python
app.include_router(auth_router)
app.include_router(video_router)
app.include_router(upload_router)
app.include_router(combine_router)
app.include_router(translate_router)
app.include_router(trim_router)
```

- [ ] **Step 3: Start the backend and test with curl**

From `backend/`:
```bash
uvicorn app.main:app --reload --port 8000
```

In another terminal (replace TOKEN with a real token from your login):
```bash
# Test status endpoint returns 404 for unknown job
curl -s -H "Authorization: Bearer TOKEN" http://localhost:8000/trim-jobs/nonexistent | python3 -m json.tool
```
Expected: `{"detail": "Job not found"}`

```bash
# Test start endpoint with a real video file
curl -s -X POST \
  -H "Authorization: Bearer TOKEN" \
  -F "file=@/path/to/test.mp4" \
  -F "start_time=0" \
  -F "end_time=5" \
  http://localhost:8000/trim-video | python3 -m json.tool
```
Expected: `{"job_id": "<hex>", "status": "queued", "progress": 0, ...}`

- [ ] **Step 4: Commit**

```bash
git add backend/app/routers/trim.py backend/app/main.py
git commit -m "feat: add trim router with start/status/cancel endpoints"
```

---

### Task 3: Frontend API types and functions

**Files:**
- Modify: `frontend/app/api.ts`

**Interfaces:**
- Produces: `TrimJobData` (exported interface), `startTrimJob`, `getTrimJob`, `cancelTrimJob`

- [ ] **Step 1: Append trim section to api.ts**

Add the following at the end of `frontend/app/api.ts`:

```typescript
// ── Trim ──────────────────────────────────────────────────────────────────────

export interface TrimJobData {
  job_id: string;
  status: string;
  progress: number;
  result_url?: string;
  error?: string;
}

export async function startTrimJob(
  token: string,
  file: File,
  startTime: number,
  endTime: number,
  onUploadProgress?: (percent: number) => void,
): Promise<TrimJobData> {
  const form = new FormData();
  form.append("file", file);
  form.append("start_time", String(startTime));
  form.append("end_time", String(endTime));
  const res = await axios.post(`${API_URL}/trim-video`, form, {
    headers: { Authorization: `Bearer ${token}` },
    onUploadProgress: (e) => {
      if (onUploadProgress && e.total) {
        onUploadProgress(Math.round((e.loaded / e.total) * 100));
      }
    },
  });
  return res.data;
}

export async function getTrimJob(token: string, jobId: string): Promise<TrimJobData> {
  const res = await axios.get(`${API_URL}/trim-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

export async function cancelTrimJob(token: string, jobId: string): Promise<void> {
  await axios.delete(`${API_URL}/trim-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}
```

- [ ] **Step 2: Verify TypeScript compiles**

From `frontend/`:
```bash
npx tsc --noEmit
```
Expected: no errors

- [ ] **Step 3: Commit**

```bash
git add frontend/app/api.ts
git commit -m "feat: add trim API functions to api.ts"
```

---

### Task 4: Frontend jobs context — add TrimJobItem

**Files:**
- Modify: `frontend/app/jobs-context.tsx`

**Interfaces:**
- Consumes: `TrimJobData` from `./api`
- Produces: exported `TrimJobItem`, `trimJobs`, `setTrimJobs`, `trimPollRefs` on context

- [ ] **Step 1: Add TrimJobItem interface**

In `frontend/app/jobs-context.tsx`, find the import line:
```typescript
import type { CombineJobData, TranslateJobData } from "./api";
```
Change it to:
```typescript
import type { CombineJobData, TranslateJobData, TrimJobData } from "./api";
```

Then after the `TranslateJobItem` interface (around line 58), add:
```typescript
export interface TrimJobItem {
  localId: string;
  filename: string;
  uploadProgress: number;
  jobId: string;
  data: TrimJobData;
}
```

- [ ] **Step 2: Add trim fields to JobsContextValue**

In the `JobsContextValue` interface, after `translatePollRefs`, add:
```typescript
  // Trim
  trimJobs: TrimJobItem[];
  setTrimJobs: Dispatch<SetStateAction<TrimJobItem[]>>;
  trimPollRefs: MutableRefObject<Map<string, ReturnType<typeof setInterval>>>;
```

- [ ] **Step 3: Add state, restore, and persist in JobsProvider**

Inside `JobsProvider`, after the `translateJobs` / `translatePollRefs` lines:

**State declaration** (after `const [translateJobs, setTranslateJobs] = ...`):
```typescript
  const [trimJobs, setTrimJobs] = useState<TrimJobItem[]>([]);
  const trimPollRefs = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
```

**localStorage restore** (inside the initial `useEffect`, after the `vidq_translate` block):
```typescript
    try {
      const raw = localStorage.getItem("vidq_trim");
      if (raw) {
        const parsed = JSON.parse(raw) as TrimJobItem[];
        const active = parsed.filter(
          (j) => j.jobId && !TERMINAL.includes(j.data.status)
        );
        if (active.length) setTrimJobs(active);
      }
    } catch {}
```

**localStorage persist** (after the `translateJobs` persist `useEffect`):
```typescript
  useEffect(() => {
    if (!persistReady.current) return;
    const toSave = trimJobs.filter(
      (j) => j.jobId && !TERMINAL.includes(j.data.status)
    );
    localStorage.setItem("vidq_trim", JSON.stringify(toSave));
  }, [trimJobs]);
```

**Provider value** — add trim fields inside `<JobsContext.Provider value={{...}}>`:
```typescript
        trimJobs, setTrimJobs,
        trimPollRefs,
```

- [ ] **Step 4: Verify TypeScript compiles**

From `frontend/`:
```bash
npx tsc --noEmit
```
Expected: no errors

- [ ] **Step 5: Commit**

```bash
git add frontend/app/jobs-context.tsx
git commit -m "feat: add TrimJobItem to jobs context with localStorage persistence"
```

---

### Task 5: Frontend trim page + Navbar link

**Files:**
- Create: `frontend/app/trim/page.tsx`
- Modify: `frontend/src/components/Navbar.tsx`

**Interfaces:**
- Consumes: `TrimJobItem` from `../jobs-context`; `startTrimJob`, `getTrimJob`, `cancelTrimJob`, `TrimJobData` from `../api`

- [ ] **Step 1: Create the trim page**

Create `frontend/app/trim/page.tsx`:

```tsx
"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../auth-context";
import { useJobs, type TrimJobItem } from "../jobs-context";
import { startTrimJob, getTrimJob, cancelTrimJob, TrimJobData } from "../api";
import { Button } from "@/components/ui/button";
import {
  Loader2, X, Check, Download, Scissors, Clock, Ban, Trash2, Play, Pause,
} from "lucide-react";
import Navbar from "@/components/Navbar";

function formatTime(seconds: number): string {
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}:${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
  return `${String(m).padStart(2, "0")}:${String(s).padStart(2, "0")}`;
}

function parseTimeInput(str: string): number | null {
  const parts = str.split(":").map(Number);
  if (parts.some(isNaN)) return null;
  if (parts.length === 3) return parts[0] * 3600 + parts[1] * 60 + parts[2];
  if (parts.length === 2) return parts[0] * 60 + parts[1];
  if (parts.length === 1 && !isNaN(parts[0])) return parts[0];
  return null;
}

function statusMessage(item: TrimJobItem): string {
  if (item.jobId === "") {
    return item.uploadProgress < 100 ? `Uploading… ${item.uploadProgress}%` : "Waiting for server…";
  }
  const { data } = item;
  if (data.status === "queued") return "Waiting for worker…";
  if (data.status === "processing") return `Trimming… ${data.progress}%`;
  if (data.status === "done") return "Done!";
  if (data.status === "failed") return data.error || "Failed";
  return "Processing…";
}

export default function TrimPage() {
  const { token, loading } = useAuth();
  const router = useRouter();

  const [phase, setPhase] = useState<"upload" | "editor">("upload");
  const [file, setFile] = useState<File | null>(null);
  const [objectUrl, setObjectUrl] = useState<string | null>(null);
  const [duration, setDuration] = useState(0);
  const [startTime, setStartTime] = useState(0);
  const [endTime, setEndTime] = useState(0);
  const [startInput, setStartInput] = useState("00:00");
  const [endInput, setEndInput] = useState("00:00");
  const [isPreviewing, setIsPreviewing] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");

  const videoRef = useRef<HTMLVideoElement>(null);
  const previewIntervalRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const { trimJobs: jobs, setTrimJobs: setJobs, trimPollRefs: pollRefs } = useJobs();

  useEffect(() => {
    if (!loading && !token) router.replace("/login");
  }, [token, loading, router]);

  // Recovery polling: re-attach intervals for non-terminal jobs restored from localStorage
  useEffect(() => {
    if (!token) return;
    jobs.forEach((job) => {
      if (
        job.jobId &&
        !pollRefs.current.has(job.localId) &&
        job.data.status !== "done" &&
        job.data.status !== "failed" &&
        job.data.status !== "cancelled"
      ) {
        startPolling(job.localId, job.jobId);
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, jobs]);

  // Cleanup on unmount
  useEffect(() => {
    return () => {
      if (previewIntervalRef.current) clearInterval(previewIntervalRef.current);
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [objectUrl]);

  function handleFile(f: File) {
    if (!f.type.startsWith("video/")) { setError("Please select a video file."); return; }
    setError("");
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    const url = URL.createObjectURL(f);
    setFile(f);
    setObjectUrl(url);
    setPhase("editor");
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) handleFile(f);
  }

  function handleVideoLoaded() {
    const vid = videoRef.current;
    if (!vid) return;
    const d = vid.duration;
    setDuration(d);
    setStartTime(0);
    setEndTime(d);
    setStartInput(formatTime(0));
    setEndInput(formatTime(d));
  }

  function onStartSlider(val: number) {
    const clamped = Math.min(val, endTime - 0.1);
    setStartTime(clamped);
    setStartInput(formatTime(clamped));
  }

  function onEndSlider(val: number) {
    const clamped = Math.max(val, startTime + 0.1);
    setEndTime(clamped);
    setEndInput(formatTime(clamped));
  }

  function onStartInputBlur() {
    const parsed = parseTimeInput(startInput);
    if (parsed === null || parsed < 0 || parsed >= endTime) {
      setStartInput(formatTime(startTime));
      return;
    }
    setStartTime(parsed);
    setStartInput(formatTime(parsed));
  }

  function onEndInputBlur() {
    const parsed = parseTimeInput(endInput);
    if (parsed === null || parsed > duration || parsed <= startTime) {
      setEndInput(formatTime(endTime));
      return;
    }
    setEndTime(parsed);
    setEndInput(formatTime(parsed));
  }

  function setStartToCurrent() {
    if (!videoRef.current) return;
    const t = Math.min(videoRef.current.currentTime, endTime - 0.1);
    setStartTime(t);
    setStartInput(formatTime(t));
  }

  function setEndToCurrent() {
    if (!videoRef.current) return;
    const t = Math.max(videoRef.current.currentTime, startTime + 0.1);
    setEndTime(t);
    setEndInput(formatTime(t));
  }

  function handlePreview() {
    if (!videoRef.current) return;
    if (isPreviewing) {
      videoRef.current.pause();
      if (previewIntervalRef.current) clearInterval(previewIntervalRef.current);
      previewIntervalRef.current = null;
      setIsPreviewing(false);
      return;
    }
    videoRef.current.currentTime = startTime;
    videoRef.current.play();
    setIsPreviewing(true);
    const interval = setInterval(() => {
      if (!videoRef.current) { clearInterval(interval); return; }
      if (videoRef.current.currentTime >= endTime) {
        videoRef.current.pause();
        clearInterval(interval);
        previewIntervalRef.current = null;
        setIsPreviewing(false);
      }
    }, 100);
    previewIntervalRef.current = interval;
  }

  function updateJob(localId: string, patch: Partial<TrimJobItem>) {
    setJobs((prev) => prev.map((j) => j.localId === localId ? { ...j, ...patch } : j));
  }

  function startPolling(localId: string, jobId: string) {
    const id = setInterval(async () => {
      if (!token) return;
      try {
        const data = await getTrimJob(token, jobId);
        updateJob(localId, { data });
        if (data.status === "done" || data.status === "failed" || data.status === "cancelled") {
          clearInterval(id);
          pollRefs.current.delete(localId);
          if (data.status === "cancelled") {
            setJobs((prev) => prev.filter((j) => j.localId !== localId));
          }
        }
      } catch {
        // ignore transient poll errors
      }
    }, 2000);
    pollRefs.current.set(localId, id);
  }

  async function handleTrim() {
    if (!token || !file || endTime <= startTime) return;
    setError("");

    const capturedFile = file;
    const localId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;

    const initialData: TrimJobData = { job_id: "", status: "queued", progress: 0 };

    setJobs((prev) => [
      ...prev,
      { localId, filename: capturedFile.name, uploadProgress: 0, jobId: "", data: initialData },
    ]);

    try {
      const data = await startTrimJob(
        token,
        capturedFile,
        startTime,
        endTime,
        (pct) => updateJob(localId, { uploadProgress: pct }),
      );
      updateJob(localId, { jobId: data.job_id, data, uploadProgress: 100 });
      startPolling(localId, data.job_id);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Trim failed";
      updateJob(localId, { data: { ...initialData, status: "failed", error: msg } });
    }
  }

  function handleCancel(localId: string) {
    const interval = pollRefs.current.get(localId);
    if (interval) { clearInterval(interval); pollRefs.current.delete(localId); }
    const job = jobs.find((j) => j.localId === localId);
    if (job?.jobId && token) cancelTrimJob(token, job.jobId).catch(() => {});
    setJobs((prev) => prev.filter((j) => j.localId !== localId));
  }

  function handleDownload(item: TrimJobItem) {
    if (!item.data.result_url) return;
    const a = document.createElement("a");
    a.href = item.data.result_url;
    a.download = `trimmed_${item.filename}`;
    a.click();
  }

  function handleDelete(localId: string) {
    setJobs((prev) => prev.filter((j) => j.localId !== localId));
  }

  function handleChangeVideo() {
    setPhase("upload");
    setIsPreviewing(false);
    if (previewIntervalRef.current) { clearInterval(previewIntervalRef.current); previewIntervalRef.current = null; }
    if (objectUrl) URL.revokeObjectURL(objectUrl);
    setObjectUrl(null);
    setFile(null);
    setDuration(0);
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center text-white">
        <Loader2 className="animate-spin w-8 h-8 text-indigo-400" />
      </div>
    );
  }

  const startPct = duration > 0 ? startTime / duration : 0;
  const endPct = duration > 0 ? endTime / duration : 1;
  // When start handle is near the right edge, bring it on top so it stays clickable
  const startOnTop = startPct > 0.9;

  return (
    <div className="min-h-screen text-white pb-20">
      <Navbar />

      <div className="max-w-3xl mx-auto px-4 sm:px-6">
        {phase === "upload" ? (
          <div className="glass-panel p-6 md:p-8 rounded-4xl mb-8 shadow-2xl shadow-purple-500/5">
            <input
              ref={fileInputRef}
              type="file"
              accept="video/*"
              className="hidden"
              onChange={(e) => { const f = e.target.files?.[0]; if (f) handleFile(f); }}
            />
            <div
              onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
              onDragLeave={() => setDragging(false)}
              onDrop={handleDrop}
              onClick={() => fileInputRef.current?.click()}
              className={`border-2 border-dashed rounded-3xl flex flex-col items-center justify-center gap-3 py-16 cursor-pointer transition-all duration-300 ${
                dragging
                  ? "border-indigo-400 bg-indigo-500/10"
                  : "border-white/10 hover:border-indigo-500/50 hover:bg-white/5"
              }`}
            >
              <div className="w-14 h-14 rounded-full bg-indigo-500/15 flex items-center justify-center">
                <Scissors className="w-6 h-6 text-indigo-400" />
              </div>
              <p className="text-white font-medium">Drop a video here or click to browse</p>
              <p className="text-gray-400 text-sm">Select start and end points to trim</p>
            </div>
            {error && <p className="text-red-400 text-sm mt-3">{error}</p>}
          </div>
        ) : (
          <div className="glass-panel p-6 rounded-4xl mb-6 shadow-2xl shadow-purple-500/5">
            {/* Video player */}
            <video
              ref={videoRef}
              src={objectUrl ?? ""}
              onLoadedMetadata={handleVideoLoaded}
              controls
              className="w-full rounded-2xl mb-5 bg-black"
            />

            {/* Set Start / Set End buttons */}
            <div className="flex justify-between mb-3">
              <button
                onClick={setStartToCurrent}
                className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
              >
                Set Start
              </button>
              <button
                onClick={setEndToCurrent}
                className="text-xs text-indigo-400 hover:text-indigo-300 transition-colors"
              >
                Set End
              </button>
            </div>

            {/* Dual-handle range slider */}
            <div className="relative h-6 flex items-center mb-3">
              {/* Track background */}
              <div className="absolute w-full h-1.5 rounded-full bg-white/10" />
              {/* Selected range highlight */}
              <div
                className="absolute h-1.5 bg-indigo-500 rounded-full"
                style={{
                  left: `${startPct * 100}%`,
                  width: `${(endPct - startPct) * 100}%`,
                }}
              />
              {/* Start range input (transparent, full width) */}
              <input
                type="range"
                min={0}
                max={duration}
                step={0.1}
                value={startTime}
                onChange={(e) => onStartSlider(Number(e.target.value))}
                className="absolute w-full h-full opacity-0 cursor-pointer"
                style={{ zIndex: startOnTop ? 20 : 10 }}
              />
              {/* End range input (transparent, full width) */}
              <input
                type="range"
                min={0}
                max={duration}
                step={0.1}
                value={endTime}
                onChange={(e) => onEndSlider(Number(e.target.value))}
                className="absolute w-full h-full opacity-0 cursor-pointer"
                style={{ zIndex: startOnTop ? 10 : 20 }}
              />
              {/* Start handle dot */}
              <div
                className="absolute w-4 h-4 rounded-full bg-white border-2 border-indigo-400 shadow-md pointer-events-none"
                style={{ left: `${startPct * 100}%`, transform: "translateX(-50%)", zIndex: 30 }}
              />
              {/* End handle dot */}
              <div
                className="absolute w-4 h-4 rounded-full bg-white border-2 border-indigo-400 shadow-md pointer-events-none"
                style={{ left: `${endPct * 100}%`, transform: "translateX(-50%)", zIndex: 30 }}
              />
            </div>

            {/* Time inputs */}
            <div className="flex justify-between mb-5">
              <input
                type="text"
                value={startInput}
                onChange={(e) => setStartInput(e.target.value)}
                onBlur={onStartInputBlur}
                className="w-24 text-center bg-white/5 border border-white/10 rounded-lg px-2 py-1 text-sm text-white focus:outline-none focus:border-indigo-500"
              />
              <input
                type="text"
                value={endInput}
                onChange={(e) => setEndInput(e.target.value)}
                onBlur={onEndInputBlur}
                className="w-24 text-center bg-white/5 border border-white/10 rounded-lg px-2 py-1 text-sm text-white focus:outline-none focus:border-indigo-500"
              />
            </div>

            {/* Action buttons */}
            <div className="flex gap-3">
              <Button
                onClick={handlePreview}
                variant="outline"
                className="flex-1 border-white/10 bg-white/5 hover:bg-white/10 text-white rounded-xl"
              >
                {isPreviewing ? (
                  <><Pause className="w-4 h-4 mr-2" />Stop</>
                ) : (
                  <><Play className="w-4 h-4 mr-2" />Preview</>
                )}
              </Button>
              <Button
                onClick={handleTrim}
                className="flex-1 bg-indigo-600 hover:bg-indigo-500 text-white rounded-xl"
              >
                <Scissors className="w-4 h-4 mr-2" />
                Trim
              </Button>
            </div>

            <button
              onClick={handleChangeVideo}
              className="mt-3 text-xs text-gray-500 hover:text-gray-300 transition-colors w-full text-center"
            >
              ← Change video
            </button>
          </div>
        )}

        {/* Job library */}
        {jobs.length > 0 && (
          <div className="space-y-2.5">
            {jobs.map((item) => {
              const visualStatus = item.jobId === "" ? "uploading" : item.data.status;
              const isDone = visualStatus === "done";
              const isFailed = visualStatus === "failed";
              const isQueued = visualStatus === "queued";
              const isActive = !isDone && !isFailed && visualStatus !== "cancelled";

              return (
                <div
                  key={item.localId}
                  className={`glass-panel px-5 py-4 rounded-2xl border flex items-center gap-4 transition-all ${
                    isDone
                      ? "border-green-500/25"
                      : isFailed
                      ? "border-red-500/25"
                      : "border-indigo-500/20"
                  }`}
                >
                  <div className="shrink-0">
                    {isDone ? (
                      <div className="w-8 h-8 rounded-full bg-green-500/15 flex items-center justify-center">
                        <Check className="w-4 h-4 text-green-400" />
                      </div>
                    ) : isFailed ? (
                      <div className="w-8 h-8 rounded-full bg-red-500/15 flex items-center justify-center">
                        <X className="w-4 h-4 text-red-400" />
                      </div>
                    ) : isQueued ? (
                      <div className="w-8 h-8 rounded-full bg-yellow-500/15 flex items-center justify-center">
                        <Clock className="w-4 h-4 text-yellow-400" />
                      </div>
                    ) : (
                      <div className="w-8 h-8 rounded-full bg-indigo-500/15 flex items-center justify-center">
                        <Loader2 className="w-4 h-4 text-indigo-400 animate-spin" />
                      </div>
                    )}
                  </div>

                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-medium text-white truncate">{item.filename}</p>
                    <p
                      className={`text-xs mt-0.5 ${
                        isDone
                          ? "text-green-400"
                          : isFailed
                          ? "text-red-400"
                          : isQueued
                          ? "text-yellow-400"
                          : "text-indigo-400"
                      }`}
                    >
                      {statusMessage(item)}
                    </p>
                    {isActive && (
                      <div className="mt-2 h-1 bg-white/5 rounded-full overflow-hidden">
                        {item.jobId === "" ? (
                          <div
                            className="h-full bg-indigo-500 rounded-full transition-all duration-300"
                            style={{ width: `${item.uploadProgress}%` }}
                          />
                        ) : item.data.progress > 0 ? (
                          <div
                            className="h-full bg-indigo-500 rounded-full transition-all duration-500"
                            style={{ width: `${item.data.progress}%` }}
                          />
                        ) : isQueued ? (
                          <div className="h-full rounded-full animate-pulse w-full bg-yellow-500/60" />
                        ) : (
                          <div className="h-full w-full bg-linear-to-r from-indigo-500/0 via-indigo-500/60 to-indigo-500/0 animate-pulse rounded-full" />
                        )}
                      </div>
                    )}
                  </div>

                  <div className="flex items-center gap-2 shrink-0">
                    {isDone && (
                      <button
                        title="Download"
                        onClick={() => handleDownload(item)}
                        className="h-8 w-8 flex items-center justify-center rounded-full bg-indigo-500/10 text-indigo-400 hover:bg-indigo-500 hover:text-white border border-indigo-500/20 transition-all"
                      >
                        <Download className="w-4 h-4" />
                      </button>
                    )}
                    {isDone && (
                      <button
                        title="Delete"
                        onClick={() => handleDelete(item.localId)}
                        className="h-8 w-8 flex items-center justify-center rounded-full bg-red-500/10 text-red-400 hover:bg-red-500 hover:text-white border border-red-500/20 transition-all"
                      >
                        <Trash2 className="w-4 h-4" />
                      </button>
                    )}
                    {isActive && (
                      <button
                        title="Cancel"
                        onClick={() => handleCancel(item.localId)}
                        className="text-gray-500 hover:text-red-400 transition-colors"
                      >
                        <Ban className="w-4 h-4" />
                      </button>
                    )}
                    {isFailed && (
                      <button
                        onClick={() => handleDelete(item.localId)}
                        className="text-gray-500 hover:text-white transition-colors"
                      >
                        <X className="w-4 h-4" />
                      </button>
                    )}
                  </div>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
```

- [ ] **Step 2: Add Trim to Navbar**

In `frontend/src/components/Navbar.tsx`, find the `NAV_LINKS` array:
```typescript
const NAV_LINKS = [
  { href: "/", label: "Download" },
  { href: "/upload", label: "Convert" },
  { href: "/combine", label: "Combine" },
  { href: "/translate", label: "Translate" },
];
```

Add the Trim entry:
```typescript
const NAV_LINKS = [
  { href: "/", label: "Download" },
  { href: "/upload", label: "Convert" },
  { href: "/combine", label: "Combine" },
  { href: "/translate", label: "Translate" },
  { href: "/trim", label: "Trim" },
];
```

- [ ] **Step 3: Verify TypeScript compiles**

From `frontend/`:
```bash
npx tsc --noEmit
```
Expected: no errors

- [ ] **Step 4: Start both servers and test end-to-end**

Terminal 1 (backend):
```bash
cd backend && uvicorn app.main:app --reload --port 8000
```

Terminal 2 (frontend):
```bash
cd frontend && npm run dev
```

Manual test checklist:
1. Navigate to `http://localhost:3000/trim` — "Trim" appears in navbar, active when on this page
2. Drop a video file → editor phase appears with video player
3. Video plays with native controls
4. "Set Start" and "Set End" buttons capture current playback position
5. Drag left slider handle → start time input updates
6. Drag right slider handle → end time input updates
7. Edit a time input and tab away → slider handle snaps to new value; invalid input reverts
8. Click Preview → video plays from start to end time and stops automatically
9. Click Stop during preview → video pauses immediately
10. Click Trim → job appears in library with upload progress, then "Waiting for worker…", then "Trimming… N%", then "Done!"
11. Click Download → trimmed file saves with correct duration
12. Click "← Change video" → returns to drop zone
13. Refresh page with in-progress job → job reappears and polling resumes

- [ ] **Step 5: Commit**

```bash
git add frontend/app/trim/page.tsx frontend/src/components/Navbar.tsx
git commit -m "feat: add trim page with dual-handle editor and library"
```
