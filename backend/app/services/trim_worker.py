import os
import queue
import subprocess
import threading
import logging
from typing import Optional

import imageio_ffmpeg

from app.config import get_settings
from app.services.ffmpeg_utils import output_file_is_valid, run_progress_process
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

logger = logging.getLogger("TrimWorker")

MAX_WORKERS = 5

_jobs: dict[str, "TrimJob"] = {}
_lock = threading.Lock()
_task_queue: queue.Queue = queue.Queue()

_pool_state = WorkerPoolState()


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
    ensure_worker_pool(
        _pool_state,
        max_workers=MAX_WORKERS,
        target=_worker_loop,
        name_prefix="trim-worker",
        logger=logger,
        label="Trim",
    )


def _worker_loop() -> None:
    while True:
        job_id = _task_queue.get()
        try:
            process_queued_job(
                job_id=job_id,
                jobs=_jobs,
                lock=_lock,
                logger=logger,
                cleanup_cancelled=lambda job: cleanup_paths([job.input_path]) if job else None,
                picked_message=lambda job: f"[{job.job_id}] Worker picked up trim job",
                process=lambda job: _process_job(job.job_id),
            )
        except Exception as e:
            logger.error(f"Trim worker loop error for {job_id}: {e}", exc_info=True)
        finally:
            _task_queue.task_done()


def get_job(job_id: str) -> Optional[TrimJob]:
    return get_registered_job(_jobs, _lock, job_id)


def cancel_job(job_id: str) -> bool:
    return cancel_registered_job(_jobs, _lock, job_id)


def start_trim_job(input_path: str, start_time: float, end_time: float) -> str:
    _ensure_pool()
    job_id = new_job_id()
    job = TrimJob(job_id=job_id, input_path=input_path, start_time=start_time, end_time=end_time)
    enqueue_registered_job(_jobs, _lock, _task_queue, job)
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
        def update_progress(current_s: float) -> None:
            if duration <= 0:
                return
            with _lock:
                job.progress = min(99, int(current_s / duration * 100))

        result = run_progress_process(
            cmd=cmd,
            job=job,
            lock=_lock,
            popen=subprocess.Popen,
            on_progress=update_progress,
        )

        if result.cancelled:
            cleanup_paths([out_path])
            return

        if result.returncode != 0 or not output_file_is_valid(out_path):
            logger.error(f"[{job_id}] ffmpeg failed: {result.stderr[-400:]}")
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
        cleanup_paths([job.input_path])
