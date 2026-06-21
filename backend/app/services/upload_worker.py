import os
import queue
import subprocess
import threading
import logging
from typing import Optional

import imageio_ffmpeg

from app.config import get_settings
from app.db import SessionLocal, Video
from app.services.ffmpeg_utils import output_file_is_valid, probe_duration, run_progress_process
from app.services.video_utils import probe_video_dimensions
from app.services.worker_runtime import (
    WorkerPoolState,
    cancel_registered_job,
    cleanup_paths,
    enqueue_registered_job,
    ensure_worker_pool,
    get_registered_job,
    new_job_id,
    process_queued_job,
)

logger = logging.getLogger("UploadWorker")

MAX_WORKERS = 5

_jobs: dict[str, "UploadJob"] = {}
_lock = threading.Lock()
_task_queue: queue.Queue = queue.Queue()

# Pool workers are started once and live for the process lifetime.
_pool_state = WorkerPoolState()


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
    ensure_worker_pool(
        _pool_state,
        max_workers=MAX_WORKERS,
        target=_worker_loop,
        name_prefix="upload-worker",
        logger=logger,
        label="Upload",
    )


def _worker_loop() -> None:
    """One persistent worker: pull jobs from the queue and process them sequentially."""
    while True:
        job_id, file_path, original_name = _task_queue.get()
        try:
            process_queued_job(
                job_id=job_id,
                jobs=_jobs,
                lock=_lock,
                logger=logger,
                cleanup_cancelled=lambda job: cleanup_paths([file_path]),
                picked_message=lambda job: f"[{job.job_id}] Worker picked up {original_name}",
                process=lambda job: _process_job(job.job_id, file_path, original_name),
            )
        except Exception as e:
            logger.error(f"Worker loop error for {job_id}: {e}", exc_info=True)
        finally:
            _task_queue.task_done()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_job(job_id: str) -> Optional[UploadJob]:
    return get_registered_job(_jobs, _lock, job_id)


def cancel_job(job_id: str) -> bool:
    """Cancel a queued or processing job. Returns True if cancelled."""
    return cancel_registered_job(_jobs, _lock, job_id)


def start_upload_job(file_path: str, original_name: str) -> str:
    """Save job metadata, enqueue it, return the job_id immediately."""
    _ensure_pool()
    job_id = new_job_id()
    job = UploadJob(job_id=job_id, filename=original_name)
    enqueue_registered_job(_jobs, _lock, _task_queue, job, (job_id, file_path, original_name))
    logger.info(f"[{job_id}] Queued: {original_name} (queue depth: {_task_queue.qsize()})")
    return job_id


# ---------------------------------------------------------------------------
# Internal processing
# ---------------------------------------------------------------------------

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
    def update_progress(current_s: float) -> None:
        if not total_duration_s or total_duration_s <= 0:
            return
        with _lock:
            job.scale_progress = min(99, int(current_s / total_duration_s * 100))

    result = run_progress_process(
        cmd=cmd,
        job=job,
        lock=_lock,
        popen=subprocess.Popen,
        on_progress=update_progress,
    )

    if result.cancelled:
        cleanup_paths([file_path, out_path])
        return None

    if result.returncode != 0 or not output_file_is_valid(out_path):
        logger.error(f"[{job.job_id}] ffmpeg failed: {result.stderr[-200:]}")
        with _lock:
            job.status = "failed"
            job.error = failure_label
        cleanup_paths([file_path])
        return None

    cleanup_paths([file_path])
    with _lock:
        job.scale_progress = 100
    logger.info(f"[{job.job_id}] {complete_label} → {os.path.basename(out_path)}")
    return out_path


def _process_job(job_id: str, file_path: str, original_name: str) -> None:
    job = _jobs[job_id]
    settings = get_settings()
    title = os.path.splitext(original_name)[0]

    try:
        duration = probe_duration(file_path)
        final_path = _scale_to_720p(job, file_path, total_duration_s=duration)
        if final_path is None:
            return  # cancelled or failed — status already set

        if final_path != file_path:
            duration = probe_duration(final_path) or duration
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
        cleanup_paths([file_path])
