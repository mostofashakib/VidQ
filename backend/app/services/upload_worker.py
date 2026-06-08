import os
import queue
import re
import subprocess
import threading
import logging
from typing import Optional

import imageio_ffmpeg

from app.config import get_settings
from app.db import SessionLocal, Video
from app.services.video_utils import probe_video_dimensions

logger = logging.getLogger("UploadWorker")

MAX_WORKERS = 5

_jobs: dict[str, "UploadJob"] = {}
_lock = threading.Lock()
_task_queue: queue.Queue = queue.Queue()

# Pool workers are started once and live for the process lifetime.
_pool_started = False
_pool_lock = threading.Lock()


class UploadJob:
    def __init__(self, job_id: str, filename: str):
        self.job_id = job_id
        self.filename = filename
        self.status = "queued"  # queued | processing | done | failed | cancelled
        self.video_id: Optional[int] = None
        self.error: Optional[str] = None
        self.scale_progress: int = 0  # 0-100, updated during ffmpeg scaling
        self._proc: Optional[subprocess.Popen] = None


# ---------------------------------------------------------------------------
# Pool management
# ---------------------------------------------------------------------------

def _ensure_pool() -> None:
    global _pool_started
    with _pool_lock:
        if _pool_started:
            return
        for i in range(MAX_WORKERS):
            t = threading.Thread(target=_worker_loop, name=f"upload-worker-{i}", daemon=True)
            t.start()
        _pool_started = True
        logger.info(f"Upload worker pool started ({MAX_WORKERS} workers)")


def _worker_loop() -> None:
    """One persistent worker: pull jobs from the queue and process them sequentially."""
    while True:
        job_id, file_path, original_name = _task_queue.get()
        try:
            job = _jobs.get(job_id)
            if not job or job.status == "cancelled":
                # Cancelled before a worker picked it up — clean up the file.
                try:
                    if os.path.exists(file_path):
                        os.remove(file_path)
                except OSError:
                    pass
                logger.info(f"[{job_id}] Skipped (cancelled before pickup)")
                continue

            with _lock:
                job.status = "processing"
            logger.info(f"[{job_id}] Worker picked up {original_name}")
            _process_job(job_id, file_path, original_name)
        except Exception as e:
            logger.error(f"Worker loop error for {job_id}: {e}", exc_info=True)
        finally:
            _task_queue.task_done()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_job(job_id: str) -> Optional[UploadJob]:
    with _lock:
        return _jobs.get(job_id)


def cancel_job(job_id: str) -> bool:
    """Cancel a queued or processing job. Returns True if cancelled."""
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


def start_upload_job(file_path: str, original_name: str) -> str:
    """Save job metadata, enqueue it, return the job_id immediately."""
    import uuid
    _ensure_pool()
    job_id = uuid.uuid4().hex
    job = UploadJob(job_id=job_id, filename=original_name)
    with _lock:
        _jobs[job_id] = job
    _task_queue.put((job_id, file_path, original_name))
    logger.info(f"[{job_id}] Queued: {original_name} (queue depth: {_task_queue.qsize()})")
    return job_id


# ---------------------------------------------------------------------------
# Internal processing
# ---------------------------------------------------------------------------

def _probe_duration(path: str) -> Optional[float]:
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        result = subprocess.run([ffmpeg_exe, "-i", path], capture_output=True, text=True)
        match = re.search(r"Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)", result.stderr)
        if match:
            h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
            return h * 3600 + m * 60 + s
    except Exception:
        pass
    return None


def _scale_to_720p(job: UploadJob, file_path: str, total_duration_s: Optional[float] = None) -> Optional[str]:
    """
    Normalize uploaded videos.

    - WebM uploads are always transcoded to MP4 for browser/download consistency.
    - Non-720p videos keep the existing 720p scaling behavior.
    Returns final path, or None on cancel/failure.
    """
    dims = probe_video_dimensions(file_path)
    base, ext = os.path.splitext(file_path)
    ext_lower = ext.lower()
    is_webm = ext_lower == ".webm"
    needs_scale = dims is not None and dims[1] != 720

    if not is_webm and not needs_scale:
        return file_path

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    suffix = "_720p" if needs_scale else "_mp4"
    out_ext = ".mp4" if is_webm else ext
    out_path = f"{base}{suffix}{out_ext}"
    cmd = [
        ffmpeg_exe, "-y", "-i", file_path,
    ]
    if needs_scale:
        cmd.extend(["-vf", "scale=-2:720:flags=lanczos"])
    cmd.extend([
        "-c:v", "libx264", "-crf", "18", "-preset", "slow",
        "-c:a", "aac" if is_webm else "copy",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        out_path,
    ])

    actions = []
    if needs_scale:
        actions.append("scaling to 720p")
    if is_webm:
        actions.append("converting WebM to MP4")
    action_label = " and ".join(actions) or "processing"
    failure_label = (
        "Could not convert WebM to MP4"
        if is_webm
        else "Video scaling failed"
    )
    complete_label = "Converted" if is_webm else "Scaled"

    logger.info(
        f"[{job.job_id}] {action_label.capitalize()}: "
        f"{os.path.basename(file_path)} → {os.path.basename(out_path)}"
    )
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

    # Drain stderr in background to prevent pipe deadlock; collect for error reporting
    stderr_lines: list[str] = []

    def _drain_stderr() -> None:
        try:
            for line in proc.stderr:
                stderr_lines.append(line)
        except Exception:
            pass

    stderr_thread = threading.Thread(target=_drain_stderr, daemon=True)
    stderr_thread.start()

    # Read stdout line-by-line for real-time progress
    try:
        for line in proc.stdout:
            line = line.strip()
            if line.startswith("out_time=") and total_duration_s and total_duration_s > 0:
                time_str = line[len("out_time="):]
                try:
                    parts = time_str.split(":")
                    current_s = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                    pct = min(99, int(current_s / total_duration_s * 100))
                    with _lock:
                        job.scale_progress = pct
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
        for p in (file_path, out_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        return None

    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
        stderr_text = "".join(stderr_lines)
        logger.error(f"[{job.job_id}] ffmpeg failed: {stderr_text[-200:]}")
        with _lock:
            job.status = "failed"
            job.error = failure_label
        try:
            os.remove(file_path)
        except OSError:
            pass
        return None

    try:
        os.remove(file_path)
    except OSError:
        pass
    with _lock:
        job.scale_progress = 100
    logger.info(f"[{job.job_id}] {complete_label} → {os.path.basename(out_path)}")
    return out_path


def _process_job(job_id: str, file_path: str, original_name: str) -> None:
    job = _jobs[job_id]
    settings = get_settings()
    title = os.path.splitext(original_name)[0]

    try:
        duration = _probe_duration(file_path)
        final_path = _scale_to_720p(job, file_path, total_duration_s=duration)
        if final_path is None:
            return  # cancelled or failed — status already set

        if final_path != file_path:
            duration = _probe_duration(final_path) or duration
        final_filename = os.path.basename(final_path)
        video_url = f"{settings.base_url}/temp_storage/{final_filename}"

        db = SessionLocal()
        try:
            db_video = Video(
                url=video_url.strip().lower(),
                category="uploads",
                title=title,
                duration=duration,
                source="upload",
            )
            db.add(db_video)
            db.commit()
            db.refresh(db_video)
            with _lock:
                if job.status == "processing":
                    job.status = "done"
                    job.video_id = db_video.id
            logger.info(f"[{job_id}] Done: video_id={db_video.id}")
        finally:
            db.close()

    except Exception as e:
        logger.error(f"[{job_id}] Process error: {e}", exc_info=True)
        with _lock:
            if job.status == "processing":
                job.status = "failed"
                job.error = str(e)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError:
            pass
