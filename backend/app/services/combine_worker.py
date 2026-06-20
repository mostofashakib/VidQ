import os
import queue
import re
import subprocess
import threading
import logging
from typing import Optional

import imageio_ffmpeg

from app.config import get_settings
from app.services.video_utils import ensure_min_quality, probe_video_dimensions

logger = logging.getLogger("CombineWorker")

MAX_WORKERS = 5

_jobs: dict[str, "CombineJob"] = {}
_lock = threading.Lock()
_task_queue: queue.Queue = queue.Queue()

_pool_started = False
_pool_lock = threading.Lock()


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
    global _pool_started
    with _pool_lock:
        if _pool_started:
            return
        for i in range(MAX_WORKERS):
            t = threading.Thread(target=_worker_loop, name=f"combine-worker-{i}", daemon=True)
            t.start()
        _pool_started = True
        logger.info(f"Combine worker pool started ({MAX_WORKERS} workers)")


def _worker_loop() -> None:
    while True:
        job_id, file_paths, filenames = _task_queue.get()
        try:
            job = _jobs.get(job_id)
            if not job or job.status == "cancelled":
                for p in file_paths:
                    try:
                        if os.path.exists(p):
                            os.remove(p)
                    except OSError:
                        pass
                logger.info(f"[{job_id}] Skipped (cancelled before pickup)")
                continue
            with _lock:
                job.status = "processing"
            logger.info(f"[{job_id}] Worker picked up {len(file_paths)} clips")
            _process_job(job_id, file_paths, filenames)
        except Exception as e:
            logger.error(f"Combine worker loop error for {job_id}: {e}", exc_info=True)
        finally:
            _task_queue.task_done()


def get_job(job_id: str) -> Optional[CombineJob]:
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


def start_combine_job(file_paths: list[str], filenames: list[str]) -> str:
    import uuid
    _ensure_pool()
    job_id = uuid.uuid4().hex
    job = CombineJob(job_id=job_id, filenames=filenames)
    with _lock:
        _jobs[job_id] = job
    _task_queue.put((job_id, file_paths, filenames))
    logger.info(f"[{job_id}] Queued: {len(filenames)} clips")
    return job_id


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
                if line.startswith("out_time=") and total_duration > 0:
                    time_str = line[len("out_time="):]
                    try:
                        parts = time_str.split(":")
                        current_s = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                        pct = min(99, int(40 + current_s / total_duration * 59))
                        with _lock:
                            job.overall_progress = pct
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
        for path in file_paths:
            try:
                if os.path.exists(path):
                    os.remove(path)
            except OSError:
                pass
