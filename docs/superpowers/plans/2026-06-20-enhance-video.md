# Enhance Video Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an Enhance page where users upload old/low-quality video and get AI-restored output using Real-ESRGAN (`realesrgan-x4plus` model) — removing noise, grain, blur, and compression artifacts so footage looks like clean HD.

**Architecture:** Backend chunks the video into 60s segments, runs ffmpeg frame extraction → realesrgan-ncnn-vulkan AI upscale → ffmpeg reassembly per chunk, then concatenates all chunks and muxes original audio back. Frontend follows the same upload → poll → library pattern as Trim, but with no editor phase — drop file, click Enhance, watch phase-labelled progress in the library. Poll interval is 3s (jobs run for minutes to hours).

**Tech Stack:** Next.js (App Router, TypeScript), FastAPI, ffmpeg via imageio_ffmpeg, realesrgan-ncnn-vulkan (brew install), subprocess.Popen, axios, lucide-react, Tailwind CSS

## Global Constraints

- Workers in `backend/app/services/`, routers in `backend/app/routers/`
- No `/api/` prefix on routes: `/enhance-video`, `/enhance-jobs/{job_id}`
- Global semaphore: `from app.services.global_semaphore import global_job_semaphore`
- Temp storage: `settings.temp_storage_dir`; result URL: `f"{settings.base_url}/temp_storage/{filename}"`
- localStorage key: `vidq_enhance`; poll interval: 3000ms
- Output: H.264 CRF 18, preset slow, pix_fmt yuv420p, scale `-2:max(720, original_height)` rounded to even
- realesrgan model: `realesrgan-x4plus`, scale `4`, tile `128`, threads `-j 1:4:1`
- Chunk duration: 60 seconds; frames extracted as JPEG `-qscale:v 2`

---

## File Structure

| File | Action | Purpose |
|------|--------|---------|
| `backend/app/services/enhance_worker.py` | Create | Thread pool, EnhanceJob, chunked AI pipeline |
| `backend/app/routers/enhance.py` | Create | FastAPI router: start, status, cancel endpoints |
| `backend/app/main.py` | Modify | Register enhance router |
| `backend/tests/test_enhance.py` | Create | Route + worker integration tests |
| `frontend/app/api.ts` | Modify | EnhanceJobData interface + 3 API functions |
| `frontend/app/jobs-context.tsx` | Modify | EnhanceJobItem, state, localStorage |
| `frontend/app/enhance/page.tsx` | Create | Upload panel + job library |
| `frontend/src/components/Navbar.tsx` | Modify | Add Enhance nav link |

---

### Task 1: Backend enhance worker

**Files:**
- Create: `backend/app/services/enhance_worker.py`

**Interfaces:**
- Produces: `start_enhance_job(input_path: str) -> str`, `get_job(job_id: str) -> Optional[EnhanceJob]`, `cancel_job(job_id: str) -> bool`
- `EnhanceJob` fields: `job_id`, `input_path`, `status` (`queued|processing|done|failed|cancelled`), `phase` (`queued|splitting|enhancing N/M|assembling|done`), `progress: int`, `error: Optional[str]`, `result_url: Optional[str]`, `_proc: Optional[subprocess.Popen]`

- [ ] **Step 1: Create the worker file**

```python
# backend/app/services/enhance_worker.py
import glob
import os
import queue
import re
import shutil
import subprocess
import threading
import logging
import uuid
from typing import Optional

import imageio_ffmpeg

from app.config import get_settings

logger = logging.getLogger("EnhanceWorker")

MAX_WORKERS = 5
CHUNK_DURATION = 60

_jobs: dict[str, "EnhanceJob"] = {}
_lock = threading.Lock()
_task_queue: queue.Queue = queue.Queue()
_pool_started = False
_pool_lock = threading.Lock()


class EnhanceJob:
    def __init__(self, job_id: str, input_path: str):
        self.job_id = job_id
        self.input_path = input_path
        self.status = "queued"   # queued | processing | done | failed | cancelled
        self.phase = "queued"    # queued | splitting | enhancing N/M | assembling | done
        self.progress: int = 0
        self.error: Optional[str] = None
        self.result_url: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None


class _CancelledError(Exception):
    pass


def _ensure_pool() -> None:
    global _pool_started
    with _pool_lock:
        if _pool_started:
            return
        for i in range(MAX_WORKERS):
            t = threading.Thread(target=_worker_loop, name=f"enhance-worker-{i}", daemon=True)
            t.start()
        _pool_started = True
        logger.info(f"Enhance worker pool started ({MAX_WORKERS} workers)")


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
                _process_job(job_id)
            finally:
                global_job_semaphore.release()
        except Exception as e:
            logger.error(f"Enhance worker loop error for {job_id}: {e}", exc_info=True)
        finally:
            _task_queue.task_done()


def get_job(job_id: str) -> Optional[EnhanceJob]:
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


def start_enhance_job(input_path: str) -> str:
    _ensure_pool()
    job_id = uuid.uuid4().hex
    job = EnhanceJob(job_id=job_id, input_path=input_path)
    with _lock:
        _jobs[job_id] = job
    _task_queue.put(job_id)
    logger.info(f"[{job_id}] Queued enhance job")
    return job_id


def _probe_video(input_path: str) -> tuple[float, float, int, int]:
    """Returns (fps, duration_seconds, width, height). Raises RuntimeError on failure."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run(
        [ffmpeg_exe, "-i", input_path],
        capture_output=True,
        text=True,
    )
    stderr = result.stderr

    fps_match = re.search(r'(\d+(?:\.\d+)?)\s*fps', stderr)
    fps = float(fps_match.group(1)) if fps_match else 30.0

    dur_match = re.search(r'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)', stderr)
    if not dur_match:
        raise RuntimeError(f"Could not parse video duration")
    h, m, s = int(dur_match.group(1)), int(dur_match.group(2)), float(dur_match.group(3))
    duration = h * 3600 + m * 60 + s

    dim_match = re.search(r'Video:.*?(\d{2,5})x(\d{2,5})', stderr)
    width = int(dim_match.group(1)) if dim_match else 640
    height = int(dim_match.group(2)) if dim_match else 480

    return fps, duration, width, height


def _run_subprocess(job_id: str, cmd: list[str]) -> None:
    """Run cmd via Popen, store handle in job._proc. Raises _CancelledError or RuntimeError."""
    job = _jobs[job_id]
    stderr_lines: list[str] = []

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    with _lock:
        job._proc = proc

    def _drain() -> None:
        try:
            for line in proc.stderr:
                stderr_lines.append(line)
        except Exception:
            pass

    t = threading.Thread(target=_drain, daemon=True)
    t.start()
    try:
        for _ in proc.stdout:
            pass
    except Exception:
        pass

    proc.wait()
    t.join(timeout=2)

    with _lock:
        job._proc = None
        cancelled = job.status == "cancelled"

    if cancelled:
        raise _CancelledError()
    if proc.returncode != 0:
        raise RuntimeError(
            f"Subprocess failed (rc={proc.returncode}): {''.join(stderr_lines[-5:])[-300:]}"
        )


def _process_job(job_id: str) -> None:
    job = _jobs[job_id]
    settings = get_settings()
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    tmp_base = os.path.join(settings.temp_storage_dir, f"{job_id}_work")
    chunks_dir = os.path.join(tmp_base, "chunks")
    out_filename = f"enhanced_{job_id}.mp4"
    out_path = os.path.join(settings.temp_storage_dir, out_filename)

    try:
        # 1. Binary check
        if not shutil.which("realesrgan-ncnn-vulkan"):
            with _lock:
                job.status = "failed"
                job.error = (
                    "realesrgan-ncnn-vulkan not found. "
                    "Install with: brew install realesrgan-ncnn-vulkan"
                )
            return

        # 2. Probe video metadata
        fps, duration, width, height = _probe_video(job.input_path)
        target_height = max(720, height)
        if target_height % 2 != 0:
            target_height += 1

        # 3. Create temp dirs
        os.makedirs(chunks_dir, exist_ok=True)

        # 4. Update phase
        with _lock:
            job.phase = "splitting"
            job.progress = 1

        # 5. Extract audio (best-effort; no audio → proceed without it)
        audio_path = os.path.join(tmp_base, "audio.m4a")
        has_audio = False
        try:
            _run_subprocess(job_id, [
                ffmpeg_exe, "-y", "-i", job.input_path,
                "-vn", "-c:a", "aac", "-b:a", "192k",
                audio_path,
            ])
            has_audio = os.path.exists(audio_path) and os.path.getsize(audio_path) > 0
        except _CancelledError:
            raise
        except Exception as e:
            logger.info(f"[{job_id}] Audio extraction skipped: {e}")

        # 6. Split into 60s chunks (video track only)
        chunk_pattern = os.path.join(chunks_dir, "chunk_%04d.mp4")
        _run_subprocess(job_id, [
            ffmpeg_exe, "-y", "-i", job.input_path,
            "-c", "copy", "-map", "0:v",
            "-f", "segment", "-segment_time", str(CHUNK_DURATION),
            "-reset_timestamps", "1",
            chunk_pattern,
        ])

        chunk_files = sorted(glob.glob(os.path.join(chunks_dir, "chunk_*.mp4")))
        if not chunk_files:
            raise RuntimeError("No chunks created from video split")
        total_chunks = len(chunk_files)

        with _lock:
            job.progress = 5

        # 7. Process each chunk
        enhanced_chunks: list[str] = []
        for idx, chunk_path in enumerate(chunk_files):
            with _lock:
                if job.status == "cancelled":
                    raise _CancelledError()
                job.phase = f"enhancing {idx + 1}/{total_chunks}"
                job.progress = 5 + int((idx / total_chunks) * 85)

            frames_dir = os.path.join(tmp_base, f"frames_{idx:04d}")
            enhanced_dir = os.path.join(tmp_base, f"enhanced_{idx:04d}")
            enhanced_chunk = os.path.join(tmp_base, f"enhanced_chunk_{idx:04d}.mp4")

            try:
                os.makedirs(frames_dir)
                os.makedirs(enhanced_dir)

                # a. Extract frames as JPEG
                _run_subprocess(job_id, [
                    ffmpeg_exe, "-y", "-i", chunk_path,
                    "-qscale:v", "2",
                    os.path.join(frames_dir, "%08d.jpg"),
                ])

                # b. AI upscale 4× with realesrgan-x4plus
                _run_subprocess(job_id, [
                    "realesrgan-ncnn-vulkan",
                    "-i", frames_dir,
                    "-o", enhanced_dir,
                    "-n", "realesrgan-x4plus",
                    "-s", "4",
                    "-t", "128",
                    "-j", "1:4:1",
                ])

                # c. Reassemble chunk at target resolution
                _run_subprocess(job_id, [
                    ffmpeg_exe, "-y",
                    "-framerate", str(fps),
                    "-i", os.path.join(enhanced_dir, "%08d.jpg"),
                    "-vf", f"scale=-2:{target_height}",
                    "-c:v", "libx264",
                    "-crf", "18",
                    "-preset", "slow",
                    "-pix_fmt", "yuv420p",
                    enhanced_chunk,
                ])

                enhanced_chunks.append(enhanced_chunk)

            finally:
                shutil.rmtree(frames_dir, ignore_errors=True)
                shutil.rmtree(enhanced_dir, ignore_errors=True)

        # 8. Assemble all chunks + mux audio
        with _lock:
            job.phase = "assembling"
            job.progress = 92

        concat_list = os.path.join(tmp_base, "concat_list.txt")
        with open(concat_list, "w") as f:
            for c in enhanced_chunks:
                f.write(f"file '{c}'\n")

        if has_audio:
            _run_subprocess(job_id, [
                ffmpeg_exe, "-y",
                "-f", "concat", "-safe", "0", "-i", concat_list,
                "-i", audio_path,
                "-map", "0:v", "-map", "1:a",
                "-c:v", "copy", "-c:a", "copy",
                out_path,
            ])
        else:
            _run_subprocess(job_id, [
                ffmpeg_exe, "-y",
                "-f", "concat", "-safe", "0", "-i", concat_list,
                "-c", "copy",
                out_path,
            ])

        if not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
            raise RuntimeError("Assembly produced empty output")

        result_url = f"{settings.base_url}/temp_storage/{out_filename}"
        with _lock:
            job.progress = 100
            job.status = "done"
            job.result_url = result_url
            job.phase = "done"
        logger.info(f"[{job_id}] Done: {out_filename}")

    except _CancelledError:
        logger.info(f"[{job_id}] Cancelled")
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            pass

    except Exception as e:
        logger.error(f"[{job_id}] Process error: {e}", exc_info=True)
        with _lock:
            if job.status == "processing":
                job.status = "failed"
                job.error = str(e)

    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)
        try:
            if os.path.exists(job.input_path):
                os.remove(job.input_path)
        except OSError:
            pass
```

- [ ] **Step 2: Verify imports cleanly**

```bash
cd backend && python -c "from app.services.enhance_worker import start_enhance_job, get_job, cancel_job; print('OK')"
```
Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add backend/app/services/enhance_worker.py
git commit -m "feat: add enhance worker with chunked Real-ESRGAN pipeline"
```

---

### Task 2: Backend enhance router + main.py registration

**Files:**
- Create: `backend/app/routers/enhance.py`
- Modify: `backend/app/main.py`

**Interfaces:**
- Consumes: `start_enhance_job`, `get_job`, `cancel_job` from `app.services.enhance_worker`
- Produces: `POST /enhance-video`, `GET /enhance-jobs/{job_id}`, `DELETE /enhance-jobs/{job_id}`

- [ ] **Step 1: Create the router**

```python
# backend/app/routers/enhance.py
import os
import shutil
import uuid
import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.config import get_settings
from app.routers.auth import verify_token
from app.services.enhance_worker import cancel_job, get_job, start_enhance_job

logger = logging.getLogger("EnhanceRouter")

router = APIRouter()


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
):
    settings = get_settings()
    os.makedirs(settings.temp_storage_dir, exist_ok=True)

    ext = os.path.splitext(file.filename or "video.mp4")[1] or ".mp4"
    filename = f"enhance_in_{uuid.uuid4().hex}{ext}"
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

    job_id = start_enhance_job(file_path)
    logger.info(f"Enhance queued: job_id={job_id}")
    return EnhanceJobOut(job_id=job_id, status="queued")


@router.get("/enhance-jobs/{job_id}", response_model=EnhanceJobOut)
def get_enhance_status(job_id: str, token: str = Depends(verify_token)):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return EnhanceJobOut(
        job_id=job.job_id,
        status=job.status,
        phase=job.phase,
        progress=job.progress,
        result_url=job.result_url,
        error=job.error,
    )


@router.delete("/enhance-jobs/{job_id}", status_code=204)
def cancel_enhance_job(job_id: str, token: str = Depends(verify_token)):
    ok = cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or not cancellable")
    return None
```

- [ ] **Step 2: Register router in main.py**

Open `backend/app/main.py`. Add this import alongside the existing router imports:
```python
from app.routers.enhance import router as enhance_router
```

Add this after `app.include_router(trim_router)`:
```python
app.include_router(enhance_router)
```

- [ ] **Step 3: Smoke-test with curl**

```bash
cd backend && uvicorn app.main:app --reload --port 8000
```

In another terminal:
```bash
curl -s -H "Authorization: Bearer test-token" http://localhost:8000/enhance-jobs/nonexistent | python3 -m json.tool
```
Expected: `{"detail": "Job not found"}`

- [ ] **Step 4: Commit**

```bash
git add backend/app/routers/enhance.py backend/app/main.py
git commit -m "feat: add enhance router with start/status/cancel endpoints"
```

---

### Task 3: Backend tests

**Files:**
- Create: `backend/tests/test_enhance.py`

**Interfaces:**
- Consumes: routes `/enhance-video`, `/enhance-jobs/{job_id}` from task 2
- Test fixtures from `conftest.py`: `client` (FastAPI TestClient with overridden DB)

- [ ] **Step 1: Write the test file**

```python
# backend/tests/test_enhance.py
"""Tests for enhance video endpoints and worker."""
import io
import os
import shutil
import time
import pytest
from unittest.mock import patch

AUTH = {"Authorization": "Bearer test-token"}


def _fake_mp4() -> bytes:
    return b"\x00" * 2048


def _make_smart_popen():
    """FakePopen that creates expected output files based on command content."""

    def fake_popen_fn(cmd, stdout=None, stderr=None, **kwargs):
        cmd_list = list(cmd)

        class FP:
            def __init__(self):
                self.stdout = iter([])
                self.stderr = iter([])
                self.returncode = 0

            def wait(self):
                return 0

            def kill(self):
                pass

        # realesrgan: copy frames from -i dir to -o dir
        if "realesrgan-ncnn-vulkan" in cmd_list:
            i_idx = cmd_list.index("-i")
            o_idx = cmd_list.index("-o")
            in_dir = cmd_list[i_idx + 1]
            out_dir = cmd_list[o_idx + 1]
            os.makedirs(out_dir, exist_ok=True)
            if os.path.isdir(in_dir):
                for fn in os.listdir(in_dir):
                    shutil.copy(os.path.join(in_dir, fn), os.path.join(out_dir, fn))

        # segment split: create one chunk file
        elif "-segment_time" in cmd_list:
            pat = cmd_list[-1]  # e.g. /path/chunks/chunk_%04d.mp4
            chunk_dir = os.path.dirname(pat)
            os.makedirs(chunk_dir, exist_ok=True)
            with open(os.path.join(chunk_dir, "chunk_0000.mp4"), "wb") as f:
                f.write(b"fake-chunk" * 200)

        # frame extraction: write one JPEG into frames dir
        elif "-qscale:v" in cmd_list:
            pat = cmd_list[-1]  # e.g. /path/frames_0000/%08d.jpg
            frames_dir = os.path.dirname(pat)
            os.makedirs(frames_dir, exist_ok=True)
            with open(os.path.join(frames_dir, "00000001.jpg"), "wb") as f:
                f.write(b"fake-jpg" * 200)

        else:
            # Generic ffmpeg: write fake data to last arg if it looks like a file
            out = cmd_list[-1]
            if not out.startswith("-") and "%" not in out and not out.endswith(".txt"):
                parent = os.path.dirname(out)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(out, "wb") as f:
                    f.write(b"fake-data" * 200)

        return FP()

    return fake_popen_fn


# ── Route-level tests ──────────────────────────────────────────────────────

def test_enhance_creates_job(client):
    r = client.post(
        "/enhance-video",
        files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
        headers=AUTH,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "queued"
    assert "job_id" in data
    assert data["phase"] == "queued"
    assert data["progress"] == 0


def test_enhance_job_not_found(client):
    r = client.get("/enhance-jobs/nonexistent-id", headers=AUTH)
    assert r.status_code == 404


def test_cancel_enhance_job_not_found(client):
    r = client.delete("/enhance-jobs/nonexistent-id", headers=AUTH)
    assert r.status_code == 404


def test_enhance_status_returns_phase(client):
    r = client.post(
        "/enhance-video",
        files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
        headers=AUTH,
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    r2 = client.get(f"/enhance-jobs/{job_id}", headers=AUTH)
    assert r2.status_code == 200
    assert "phase" in r2.json()


# ── Worker tests ───────────────────────────────────────────────────────────

def test_enhance_missing_binary_marks_failed(client):
    """If realesrgan-ncnn-vulkan is not on PATH, job fails with a clear error."""
    with patch("app.services.enhance_worker.shutil.which", return_value=None):
        with patch("app.services.enhance_worker._probe_video", return_value=(30.0, 5.0, 640, 480)):
            r = client.post(
                "/enhance-video",
                files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
                headers=AUTH,
            )
            assert r.status_code == 200
            job_id = r.json()["job_id"]

            deadline = time.time() + 10
            result = {}
            while time.time() < deadline:
                r2 = client.get(f"/enhance-jobs/{job_id}", headers=AUTH)
                result = r2.json()
                if result["status"] in ("done", "failed"):
                    break
                time.sleep(0.1)

    assert result["status"] == "failed"
    assert "realesrgan-ncnn-vulkan" in (result.get("error") or "")


def test_enhance_job_completes(client):
    """Full pipeline with mocked subprocess produces done status and result_url."""
    with patch("app.services.enhance_worker.shutil.which",
               return_value="/usr/local/bin/realesrgan-ncnn-vulkan"):
        with patch("app.services.enhance_worker._probe_video",
                   return_value=(30.0, 5.0, 640, 480)):
            with patch("app.services.enhance_worker.subprocess.Popen",
                       side_effect=_make_smart_popen()):
                r = client.post(
                    "/enhance-video",
                    files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
                    headers=AUTH,
                )
                assert r.status_code == 200
                job_id = r.json()["job_id"]

                deadline = time.time() + 15
                result = {}
                while time.time() < deadline:
                    r2 = client.get(f"/enhance-jobs/{job_id}", headers=AUTH)
                    result = r2.json()
                    if result["status"] in ("done", "failed"):
                        break
                    time.sleep(0.1)

    assert result["status"] == "done", f"Expected done, got: {result}"
    assert result["result_url"] is not None
    assert result["progress"] == 100


def test_enhance_subprocess_failure_marks_failed(client):
    """If any subprocess exits non-zero, job is marked failed."""

    class FailingPopen:
        def __init__(self, cmd, stdout=None, stderr=None, **kwargs):
            self.stdout = iter([])
            self.stderr = iter(["error\n"])
            self.returncode = 1

        def wait(self):
            return 1

        def kill(self):
            pass

    with patch("app.services.enhance_worker.shutil.which",
               return_value="/usr/local/bin/realesrgan-ncnn-vulkan"):
        with patch("app.services.enhance_worker._probe_video",
                   return_value=(30.0, 5.0, 640, 480)):
            with patch("app.services.enhance_worker.subprocess.Popen", FailingPopen):
                r = client.post(
                    "/enhance-video",
                    files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
                    headers=AUTH,
                )
                assert r.status_code == 200
                job_id = r.json()["job_id"]

                deadline = time.time() + 10
                result = {}
                while time.time() < deadline:
                    r2 = client.get(f"/enhance-jobs/{job_id}", headers=AUTH)
                    result = r2.json()
                    if result["status"] in ("done", "failed"):
                        break
                    time.sleep(0.1)

    assert result["status"] == "failed"


def test_enhance_cancel_job(client):
    """Cancelling a job marks it cancelled."""
    r = client.post(
        "/enhance-video",
        files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
        headers=AUTH,
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    r2 = client.delete(f"/enhance-jobs/{job_id}", headers=AUTH)
    # 204 = cancelled; 404 = worker finished before cancel (race)
    assert r2.status_code in (204, 404)

    if r2.status_code == 204:
        r3 = client.get(f"/enhance-jobs/{job_id}", headers=AUTH)
        assert r3.json()["status"] == "cancelled"
```

- [ ] **Step 2: Run all tests**

```bash
cd backend && pytest tests/test_enhance.py -v
```
Expected: all tests pass (PASSED x 9). A test may show SKIPPED if the cancel race resolves before the DELETE — that is acceptable.

- [ ] **Step 3: Confirm existing tests still pass**

```bash
cd backend && pytest tests/ -v
```
Expected: all tests pass (no regressions).

- [ ] **Step 4: Commit**

```bash
git add backend/tests/test_enhance.py
git commit -m "test: add enhance endpoint and worker tests"
```

---

### Task 4: Frontend API types and functions

**Files:**
- Modify: `frontend/app/api.ts`

**Interfaces:**
- Produces: exported `EnhanceJobData`, `startEnhanceJob`, `getEnhanceJob`, `cancelEnhanceJob`

- [ ] **Step 1: Append enhance section to api.ts**

At the end of `frontend/app/api.ts`, add:

```typescript
// ── Enhance ───────────────────────────────────────────────────────────────────

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
): Promise<EnhanceJobData> {
  const form = new FormData();
  form.append("file", file);
  const res = await axios.post(`${API_URL}/enhance-video`, form, {
    headers: { Authorization: `Bearer ${token}` },
    onUploadProgress: (e) => {
      if (onUploadProgress && e.total) {
        onUploadProgress(Math.round((e.loaded / e.total) * 100));
      }
    },
  });
  return res.data;
}

export async function getEnhanceJob(token: string, jobId: string): Promise<EnhanceJobData> {
  const res = await axios.get(`${API_URL}/enhance-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}

export async function cancelEnhanceJob(token: string, jobId: string): Promise<void> {
  await axios.delete(`${API_URL}/enhance-jobs/${jobId}`, {
    headers: { Authorization: `Bearer ${token}` },
  });
}
```

- [ ] **Step 2: Verify TypeScript compiles**

```bash
cd frontend && npx tsc --noEmit
```
Expected: no errors

- [ ] **Step 3: Commit**

```bash
git add frontend/app/api.ts
git commit -m "feat: add enhance API functions to api.ts"
```

---

### Task 5: Frontend jobs context — add EnhanceJobItem

**Files:**
- Modify: `frontend/app/jobs-context.tsx`

**Interfaces:**
- Consumes: `EnhanceJobData` from `./api`
- Produces: exported `EnhanceJobItem`; `enhanceJobs`, `setEnhanceJobs`, `enhancePollRefs` on context

- [ ] **Step 1: Add EnhanceJobData to the import**

Find line 14:
```typescript
import type { CombineJobData, TranslateJobData, TrimJobData } from "./api";
```
Change to:
```typescript
import type { CombineJobData, EnhanceJobData, TranslateJobData, TrimJobData } from "./api";
```

- [ ] **Step 2: Add EnhanceJobItem interface**

After the `TrimJobItem` interface (around line 66), add:
```typescript
export interface EnhanceJobItem {
  localId: string;
  filename: string;
  uploadProgress: number;
  jobId: string;
  data: EnhanceJobData;
}
```

- [ ] **Step 3: Add enhance fields to JobsContextValue**

In the `JobsContextValue` interface, after the Trim block:
```typescript
  // Enhance
  enhanceJobs: EnhanceJobItem[];
  setEnhanceJobs: Dispatch<SetStateAction<EnhanceJobItem[]>>;
  enhancePollRefs: MutableRefObject<Map<string, ReturnType<typeof setInterval>>>;
```

- [ ] **Step 4: Add state and ref in JobsProvider**

Inside `JobsProvider`, after `const [trimJobs, setTrimJobs] = useState...`:
```typescript
  const [enhanceJobs, setEnhanceJobs] = useState<EnhanceJobItem[]>([]);
  const enhancePollRefs = useRef<Map<string, ReturnType<typeof setInterval>>>(new Map());
```

- [ ] **Step 5: Add localStorage restore in the init useEffect**

Inside the init `useEffect`, after the `vidq_trim` block:
```typescript
    try {
      const raw = localStorage.getItem("vidq_enhance");
      if (raw) {
        const parsed = JSON.parse(raw) as EnhanceJobItem[];
        const active = parsed.filter(
          (j) => j.jobId && !TERMINAL.includes(j.data.status)
        );
        if (active.length) setEnhanceJobs(active);
      }
    } catch {}
```

- [ ] **Step 6: Add localStorage persist useEffect**

After the `trimJobs` persist `useEffect`:
```typescript
  useEffect(() => {
    if (!persistReady.current) return;
    const toSave = enhanceJobs.filter(
      (j) => j.jobId && !TERMINAL.includes(j.data.status)
    );
    localStorage.setItem("vidq_enhance", JSON.stringify(toSave));
  }, [enhanceJobs]);
```

- [ ] **Step 7: Add enhance fields to the context provider value**

Inside `<JobsContext.Provider value={{...}}>`, after `trimJobs, setTrimJobs, trimPollRefs,`:
```typescript
        enhanceJobs, setEnhanceJobs,
        enhancePollRefs,
```

- [ ] **Step 8: Verify TypeScript compiles**

```bash
cd frontend && npx tsc --noEmit
```
Expected: no errors

- [ ] **Step 9: Commit**

```bash
git add frontend/app/jobs-context.tsx
git commit -m "feat: add EnhanceJobItem to jobs context with localStorage persistence"
```

---

### Task 6: Frontend enhance page + Navbar link

**Files:**
- Create: `frontend/app/enhance/page.tsx`
- Modify: `frontend/src/components/Navbar.tsx`

**Interfaces:**
- Consumes: `EnhanceJobItem` from `../jobs-context`; `startEnhanceJob`, `getEnhanceJob`, `cancelEnhanceJob`, `EnhanceJobData` from `../api`

- [ ] **Step 1: Create the enhance page**

Create `frontend/app/enhance/page.tsx`:

```tsx
"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../auth-context";
import { useJobs, type EnhanceJobItem } from "../jobs-context";
import { startEnhanceJob, getEnhanceJob, cancelEnhanceJob, type EnhanceJobData } from "../api";
import { Button } from "@/components/ui/button";
import { Loader2, X, Check, Download, Sparkles, Clock, Ban, Trash2 } from "lucide-react";
import Navbar from "@/components/Navbar";

function statusMessage(item: EnhanceJobItem): string {
  if (item.jobId === "") {
    return item.uploadProgress < 100
      ? `Uploading… ${item.uploadProgress}%`
      : "Waiting for server…";
  }
  const { data } = item;
  if (data.status === "queued") return "Queued…";
  if (data.status === "processing") {
    const phase = data.phase;
    if (phase === "splitting") return "Splitting video…";
    if (phase === "assembling") return "Assembling final video…";
    if (phase.startsWith("enhancing")) {
      const chunk = phase.split(" ")[1] ?? "1/1";
      const [n, total] = chunk.split("/");
      return `Enhancing chunk ${n} of ${total} — ${data.progress}%`;
    }
    return `Processing… ${data.progress}%`;
  }
  if (data.status === "done") return "Done!";
  if (data.status === "failed") return data.error || "Enhancement failed";
  return "Processing…";
}

export default function EnhancePage() {
  const { token, loading } = useAuth();
  const router = useRouter();

  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const fileInputRef = useRef<HTMLInputElement>(null);

  const {
    enhanceJobs: jobs,
    setEnhanceJobs: setJobs,
    enhancePollRefs: pollRefs,
  } = useJobs();

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

  function handleFile(f: File) {
    if (!f.type.startsWith("video/")) {
      setError("Please select a video file.");
      return;
    }
    setError("");
    setFile(f);
  }

  function handleDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    const f = e.dataTransfer.files[0];
    if (f) handleFile(f);
  }

  function updateJob(localId: string, patch: Partial<EnhanceJobItem>) {
    setJobs((prev) => prev.map((j) => (j.localId === localId ? { ...j, ...patch } : j)));
  }

  function startPolling(localId: string, jobId: string) {
    const id = setInterval(async () => {
      if (!token) return;
      try {
        const data = await getEnhanceJob(token, jobId);
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
    }, 3000);
    pollRefs.current.set(localId, id);
  }

  async function handleEnhance() {
    if (!token || !file) return;
    setError("");
    const capturedFile = file;
    const localId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
    const initialData: EnhanceJobData = {
      job_id: "",
      status: "queued",
      phase: "queued",
      progress: 0,
    };

    setJobs((prev) => [
      ...prev,
      { localId, filename: capturedFile.name, uploadProgress: 0, jobId: "", data: initialData },
    ]);
    setFile(null);

    try {
      const data = await startEnhanceJob(
        token,
        capturedFile,
        (pct) => updateJob(localId, { uploadProgress: pct }),
      );
      updateJob(localId, { jobId: data.job_id, data, uploadProgress: 100 });
      startPolling(localId, data.job_id);
    } catch (e: unknown) {
      const msg = e instanceof Error ? e.message : "Enhancement failed";
      updateJob(localId, { data: { ...initialData, status: "failed", error: msg } });
    }
  }

  function handleCancel(localId: string) {
    const interval = pollRefs.current.get(localId);
    if (interval) { clearInterval(interval); pollRefs.current.delete(localId); }
    const job = jobs.find((j) => j.localId === localId);
    if (job?.jobId && token) cancelEnhanceJob(token, job.jobId).catch(() => {});
    setJobs((prev) => prev.filter((j) => j.localId !== localId));
  }

  function handleDelete(localId: string) {
    setJobs((prev) => prev.filter((j) => j.localId !== localId));
  }

  function handleDownload(item: EnhanceJobItem) {
    if (!item.data.result_url) return;
    const a = document.createElement("a");
    a.href = item.data.result_url;
    a.download = `enhanced_${item.filename}`;
    a.click();
  }

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center text-white">
        <Loader2 className="animate-spin w-8 h-8 text-indigo-400" />
      </div>
    );
  }

  return (
    <div className="min-h-screen text-white pb-20">
      <Navbar />
      <div className="max-w-3xl mx-auto px-4 sm:px-6">

        {/* Upload panel */}
        <div className="glass-panel p-6 md:p-8 rounded-4xl mb-8 shadow-2xl shadow-purple-500/5">
          <input
            ref={fileInputRef}
            type="file"
            accept="video/*"
            className="hidden"
            onChange={(e) => {
              const f = e.target.files?.[0];
              if (f) handleFile(f);
              e.target.value = "";
            }}
          />
          <div
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={handleDrop}
            onClick={() => !file && fileInputRef.current?.click()}
            className={`border-2 border-dashed rounded-3xl flex flex-col items-center justify-center gap-3 py-16 transition-all duration-300 ${
              file
                ? "border-indigo-500/50 bg-indigo-500/5 cursor-default"
                : dragging
                ? "border-indigo-400 bg-indigo-500/10 cursor-pointer"
                : "border-white/10 hover:border-indigo-500/50 hover:bg-white/5 cursor-pointer"
            }`}
          >
            <div className="w-14 h-14 rounded-full bg-indigo-500/15 flex items-center justify-center">
              <Sparkles className="w-6 h-6 text-indigo-400" />
            </div>
            {file ? (
              <>
                <p className="text-white font-medium">{file.name}</p>
                <p className="text-gray-400 text-sm">Ready to enhance</p>
              </>
            ) : (
              <>
                <p className="text-white font-medium">Drop a video here or click to browse</p>
                <p className="text-gray-400 text-sm">
                  AI restores quality — removes noise, grain, and compression artifacts
                </p>
              </>
            )}
          </div>
          {error && <p className="text-red-400 text-sm mt-3">{error}</p>}
          <div className="flex gap-3 mt-4">
            {file && (
              <Button
                variant="outline"
                onClick={() => setFile(null)}
                className="border-white/10 bg-white/5 hover:bg-white/10 text-white rounded-xl"
              >
                Clear
              </Button>
            )}
            <Button
              onClick={handleEnhance}
              disabled={!file}
              className="flex-1 bg-indigo-600 hover:bg-indigo-500 disabled:opacity-50 text-white rounded-xl"
            >
              <Sparkles className="w-4 h-4 mr-2" />
              Enhance Video
            </Button>
          </div>
        </div>

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

- [ ] **Step 2: Add Enhance to Navbar**

In `frontend/src/components/Navbar.tsx`, find `NAV_LINKS`:
```typescript
const NAV_LINKS = [
  { href: "/", label: "Download" },
  { href: "/upload", label: "Convert" },
  { href: "/combine", label: "Combine" },
  { href: "/translate", label: "Translate" },
  { href: "/trim", label: "Trim" },
];
```

Add the Enhance entry:
```typescript
const NAV_LINKS = [
  { href: "/", label: "Download" },
  { href: "/upload", label: "Convert" },
  { href: "/combine", label: "Combine" },
  { href: "/translate", label: "Translate" },
  { href: "/trim", label: "Trim" },
  { href: "/enhance", label: "Enhance" },
];
```

- [ ] **Step 3: Verify TypeScript compiles**

```bash
cd frontend && npx tsc --noEmit
```
Expected: no errors

- [ ] **Step 4: Start servers and test end-to-end**

Terminal 1:
```bash
cd backend && uvicorn app.main:app --reload --port 8000
```

Terminal 2:
```bash
cd frontend && npm run dev
```

Manual test checklist:
1. Navigate to `http://localhost:3000/enhance` — "Enhance" appears in navbar, active on this page
2. Drop a video file → filename appears in drop zone, "Enhance Video" button enables
3. Click "Clear" → file deselects, drop zone resets
4. Drop another file, click "Enhance Video" → file clears, job appears in library with upload progress bar
5. Job transitions: upload progress → "Queued…" → "Splitting video…" → "Enhancing chunk N of M — X%" → "Assembling final video…" → "Done!" with Download button
6. Click Download → file saves as `enhanced_<filename>.mp4`
7. If `realesrgan-ncnn-vulkan` is not installed: job shows "Enhancement failed" with the install instruction error
8. Refresh mid-job → job reappears, polling resumes, progress continues from last known state
9. Cancel an in-progress job → job disappears from library

- [ ] **Step 5: Commit**

```bash
git add frontend/app/enhance/page.tsx frontend/src/components/Navbar.tsx
git commit -m "feat: add enhance page with AI restoration pipeline and library"
```
