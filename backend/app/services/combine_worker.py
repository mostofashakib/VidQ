import os
import queue
import subprocess
import threading
import logging
from typing import Optional

import imageio_ffmpeg

from app.config import get_settings
from app.services.ffmpeg_utils import output_file_is_valid, probe_duration, run_progress_process
from app.services.video_utils import ensure_min_quality, probe_video_dimensions
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

logger = logging.getLogger("CombineWorker")

MAX_WORKERS = 5

_jobs: dict[str, "CombineJob"] = {}
_lock = threading.Lock()
_task_queue: queue.Queue = queue.Queue()

_pool_state = WorkerPoolState()


class CombineJob:
    def __init__(self, job_id: str, filenames: list[str]):
        self.job_id = job_id
        self.filenames = filenames
        self.status = "queued"  # queued | processing | done | failed | cancelled
        self.error: Optional[str] = None
        self.phase = "queued"  # queued | normalizing | concatenating
        self.overall_progress: int = 0
        self.clip_index: int = 0
        self.total_clips: int = len(filenames)
        self.result_url: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None


def _ensure_pool() -> None:
    ensure_worker_pool(
        _pool_state,
        max_workers=MAX_WORKERS,
        target=_worker_loop,
        name_prefix="combine-worker",
        logger=logger,
        label="Combine",
    )


def _worker_loop() -> None:
    while True:
        job_id, file_paths, filenames = _task_queue.get()
        try:
            process_queued_job(
                job_id=job_id,
                jobs=_jobs,
                lock=_lock,
                logger=logger,
                cleanup_cancelled=lambda job: cleanup_paths(file_paths),
                picked_message=lambda job: f"[{job.job_id}] Worker picked up {len(file_paths)} clips",
                process=lambda job: _process_job(job.job_id, file_paths, filenames),
            )
        except Exception as e:
            logger.error(f"Combine worker loop error for {job_id}: {e}", exc_info=True)
        finally:
            _task_queue.task_done()


def get_job(job_id: str) -> Optional[CombineJob]:
    return get_registered_job(_jobs, _lock, job_id)


def cancel_job(job_id: str) -> bool:
    return cancel_registered_job(_jobs, _lock, job_id)


def start_combine_job(file_paths: list[str], filenames: list[str]) -> str:
    _ensure_pool()
    job_id = new_job_id()
    job = CombineJob(job_id=job_id, filenames=filenames)
    enqueue_registered_job(_jobs, _lock, _task_queue, job, (job_id, file_paths, filenames))
    logger.info(f"[{job_id}] Queued: {len(filenames)} clips")
    return job_id


def _probe_duration(path: str) -> Optional[float]:
    return probe_duration(path)


def _build_xfade_filter(durations: list[float], fade_duration: float = 0.5) -> tuple[str, str]:
    """Return (video_filter, audio_filter) strings for N clips with xfade transitions."""
    n = len(durations)
    if n == 1:
        return "[0:v]null[vout]", "[0:a]anull[aout]"

    video_parts = []
    audio_parts = []
    cumulative = 0.0

    for i in range(n - 1):
        offset = cumulative + durations[i] - fade_duration * (i + 1)
        cumulative += durations[i]

        if i == 0:
            v_in = "[0:v][1:v]"
            a_in = "[0:a][1:a]"
        else:
            v_in = f"[vx{i}][{i+1}:v]"
            a_in = f"[ax{i}][{i+1}:a]"

        v_out = "[vout]" if i == n - 2 else f"[vx{i+1}]"
        a_out = "[aout]" if i == n - 2 else f"[ax{i+1}]"

        video_parts.append(
            f"{v_in}xfade=transition=fade:duration={fade_duration}:offset={offset:.3f}{v_out}"
        )
        audio_parts.append(
            f"{a_in}acrossfade=d={fade_duration}{a_out}"
        )

    return ";".join(video_parts), ";".join(audio_parts)


def _process_job(job_id: str, file_paths: list[str], filenames: list[str]) -> None:
    job = _jobs[job_id]
    settings = get_settings()
    total = len(file_paths)
    temp_files_to_clean: list[str] = []

    try:
        # Phase 1: Normalize all clips to 720p
        with _lock:
            job.phase = "normalizing"
            job.overall_progress = 0

        normalized: list[str] = []
        for i, path in enumerate(file_paths):
            if job.status == "cancelled":
                return

            with _lock:
                job.clip_index = i + 1

            logger.info(f"[{job_id}] Normalizing clip {i+1}/{total}: {filenames[i]}")
            dims = probe_video_dimensions(path)
            if dims and dims[1] != 720:
                normed = ensure_min_quality(path)
                if normed != path:
                    temp_files_to_clean.append(normed)
                normalized.append(normed)
            else:
                normalized.append(path)

            with _lock:
                job.overall_progress = int((i + 1) / total * 40)  # 0-40%

        if job.status == "cancelled":
            return

        # Phase 2: Probe durations for xfade offsets
        with _lock:
            job.phase = "concatenating"
            job.overall_progress = 40

        durations: list[float] = []
        for path in normalized:
            d = _probe_duration(path)
            durations.append(d or 5.0)

        out_filename = f"combined_{job_id}.mp4"
        out_path = os.path.join(settings.temp_storage_dir, out_filename)

        # Phase 3: Build and run ffmpeg xfade concat
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [ffmpeg_exe, "-y"]
        for path in normalized:
            cmd.extend(["-i", path])

        total_duration = sum(durations) - (len(durations) - 1) * 0.5

        if len(normalized) == 1:
            # Single clip: just copy/re-encode to output
            cmd.extend([
                "-c:v", "libx264", "-crf", "18", "-preset", "slow",
                "-c:a", "aac", "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats",
                out_path,
            ])
        else:
            v_filter, a_filter = _build_xfade_filter(durations)
            filter_complex = f"{v_filter};{a_filter}"
            cmd.extend([
                "-filter_complex", filter_complex,
                "-map", "[vout]", "-map", "[aout]",
                "-c:v", "libx264", "-crf", "18", "-preset", "slow",
                "-c:a", "aac", "-movflags", "+faststart",
                "-progress", "pipe:1", "-nostats",
                out_path,
            ])

        logger.info(f"[{job_id}] Running ffmpeg concat ({len(normalized)} clips)")

        def update_progress(current_s: float) -> None:
            if total_duration <= 0:
                return
            with _lock:
                job.overall_progress = min(99, int(40 + current_s / total_duration * 59))

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
                job.error = "Video merge failed"
            return

        result_url = f"{settings.base_url}/temp_storage/{out_filename}"
        with _lock:
            job.overall_progress = 100
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
        # Clean up temp normalized copies (originals are kept for potential re-use)
        cleanup_paths(file_paths)
        cleanup_paths(temp_files_to_clean)
