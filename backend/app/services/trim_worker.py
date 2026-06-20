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
