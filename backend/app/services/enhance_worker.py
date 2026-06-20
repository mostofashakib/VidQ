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
