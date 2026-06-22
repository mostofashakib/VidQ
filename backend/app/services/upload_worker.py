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
    Normalize uploaded video to H.264/AAC 1280×720 MP4 regardless of source format.
    Aspect ratio is preserved with letterbox/pillarbox padding to fill exactly 1280×720.
    Returns final path, or None on cancel/failure.
    """
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    base = os.path.splitext(file_path)[0]
    out_path = f"{base}_converted.mp4"
    cmd = [
        ffmpeg_exe, "-y", "-i", file_path,
        "-vf", "scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2:color=black",
        "-c:v", "libx264", "-crf", "18", "-preset", "slow",
        "-c:a", "aac",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        out_path,
    ]

    logger.info(
        f"[{job.job_id}] Converting to 1280×720 H.264/AAC MP4: "
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
            job.error = "Video conversion failed"
        cleanup_paths([file_path])
        return None

    cleanup_paths([file_path])
    with _lock:
        job.scale_progress = 100
    logger.info(f"[{job.job_id}] Converted → {os.path.basename(out_path)}")
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
