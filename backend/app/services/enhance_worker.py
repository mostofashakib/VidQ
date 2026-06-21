import glob
import os
import queue
import re
import shutil
import subprocess
import threading
import logging
import signal
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import contextmanager
from typing import Optional
from pathlib import Path

import imageio_ffmpeg

from app.config import get_settings
from app.services.ffmpeg_utils import ProcessRunResult, run_progress_process
from app.services.global_semaphore import global_job_semaphore
from app.services.worker_runtime import (
    WorkerPoolState,
    cleanup_paths,
    enqueue_registered_job,
    ensure_worker_pool,
    get_registered_job,
    new_job_id,
    process_queued_job,
)

logger = logging.getLogger("EnhanceWorker")

MAX_WORKERS = 5
CHUNK_DURATION = 60
MAX_CHUNK_WORKERS = 5

_jobs: dict[str, "EnhanceJob"] = {}
_lock = threading.Lock()
_task_queue: queue.Queue = queue.Queue()
_pool_state = WorkerPoolState()


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
        self._procs: set[subprocess.Popen] = set()


class _CancelledError(Exception):
    pass


class _SubprocessError(RuntimeError):
    def __init__(self, returncode: int, stderr: str):
        self.returncode = returncode
        self.stderr = stderr
        super().__init__(_format_subprocess_failure(returncode, stderr))


def _real_esrgan_install_message() -> str:
    return (
        "Real-ESRGAN is not configured. Run ./setup.sh, or set "
        "REAL_ESRGAN_BIN for ncnn Vulkan / REAL_ESRGAN_PYTHON and "
        "REAL_ESRGAN_MODEL_PATH for the Python backend."
    )


def _resolve_real_esrgan_bin(configured_path: str = "") -> Optional[str]:
    if configured_path:
        configured = Path(configured_path).expanduser()
        if configured.is_file() and os.access(configured, os.X_OK):
            return str(configured)

    found = shutil.which("realesrgan-ncnn-vulkan")
    if found:
        return found

    for candidate in (
        "/opt/homebrew/bin/realesrgan-ncnn-vulkan",
        "/usr/local/bin/realesrgan-ncnn-vulkan",
    ):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate

    return None


def _resolve_real_esrgan_python(configured_path: str = "") -> Optional[str]:
    candidates = []
    if configured_path:
        candidates.append(str(Path(configured_path).expanduser()))

    service_dir = Path(__file__).resolve().parent
    backend_dir = service_dir.parent.parent
    candidates.extend([
        str(backend_dir / ".realesrgan-venv" / "bin" / "python"),
        str(backend_dir / ".venv" / "bin" / "python"),
    ])

    for candidate in candidates:
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


def _resolve_real_esrgan_model_path(
    configured_path: str = "",
) -> Optional[str]:
    candidates = []
    if configured_path:
        candidates.append(str(Path(configured_path).expanduser()))

    backend_dir = Path(__file__).resolve().parent.parent.parent
    candidates.append(str(backend_dir / "models" / "realesrgan" / "RealESRGAN_x4plus.pth"))

    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _python_backend_install_message() -> str:
    return (
        "Real-ESRGAN ncnn Vulkan crashed and the Python backend is not ready. "
        "Run ./setup.sh without SKIP_PYTHON_REALESRGAN=1, then restart ./run.sh."
    )


def _format_subprocess_failure(returncode: int, stderr: str) -> str:
    stderr_tail = stderr[-300:].strip()
    if returncode < 0:
        signal_number = -returncode
        try:
            signal_name = signal.Signals(signal_number).name
        except ValueError:
            signal_name = f"signal {signal_number}"
        message = f"Subprocess crashed ({signal_name})"
    else:
        message = f"Subprocess failed (rc={returncode})"

    if stderr_tail:
        return f"{message}: {stderr_tail}"
    return message


def _ensure_pool() -> None:
    ensure_worker_pool(
        _pool_state,
        max_workers=MAX_WORKERS,
        target=_worker_loop,
        name_prefix="enhance-worker",
        logger=logger,
        label="Enhance",
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
                picked_message=lambda job: f"[{job.job_id}] Worker picked up enhance job",
                process=lambda job: _process_job(job.job_id),
                acquire_global_slot=False,
            )
        except Exception as e:
            logger.error(f"Enhance worker loop error for {job_id}: {e}", exc_info=True)
        finally:
            _task_queue.task_done()


def get_job(job_id: str) -> Optional[EnhanceJob]:
    return get_registered_job(_jobs, _lock, job_id)


def _kill_job_processes(job: EnhanceJob) -> None:
    with _lock:
        procs = list(job._procs)
        current_proc = job._proc

    for proc in procs:
        try:
            proc.kill()
        except Exception:
            pass
    if current_proc:
        try:
            current_proc.kill()
        except Exception:
            pass


def cancel_job(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if not job or job.status in ("done", "failed", "cancelled"):
            return False
        job.status = "cancelled"
    _kill_job_processes(job)
    return True


def start_enhance_job(input_path: str) -> str:
    _ensure_pool()
    job_id = new_job_id()
    job = EnhanceJob(job_id=job_id, input_path=input_path)
    enqueue_registered_job(_jobs, _lock, _task_queue, job)
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


def _run_subprocess_result(job_id: str, cmd: list[str]) -> ProcessRunResult:
    """Run cmd via Popen, store handle in job._proc. Raises _CancelledError."""
    job = _jobs[job_id]
    result = run_progress_process(
        cmd=cmd,
        job=job,
        lock=_lock,
        popen=subprocess.Popen,
    )

    if result.cancelled:
        raise _CancelledError()
    return result


def _run_subprocess_result_isolated(
    job_id: str,
    cmd: list[str],
    *,
    cwd: str,
) -> ProcessRunResult:
    """Run helper scripts outside the app package to avoid stdlib module shadowing."""
    env = os.environ.copy()
    env.pop("PYTHONPATH", None)
    result = run_progress_process(
        cmd=cmd,
        job=_jobs[job_id],
        lock=_lock,
        popen=subprocess.Popen,
        cwd=cwd,
        env=env,
    )

    if result.cancelled:
        raise _CancelledError()
    return result


def _run_subprocess(job_id: str, cmd: list[str]) -> None:
    """Run cmd via Popen, store handle in job._proc. Raises _CancelledError or RuntimeError."""
    result = _run_subprocess_result(job_id, cmd)
    if result.returncode != 0:
        raise _SubprocessError(result.returncode, result.stderr)


def _raise_if_cancelled(job_id: str) -> None:
    with _lock:
        job = _jobs[job_id]
        if job.status == "cancelled":
            raise _CancelledError()


@contextmanager
def _global_slot(job_id: str):
    acquired = False
    while not acquired:
        _raise_if_cancelled(job_id)
        acquired = global_job_semaphore.acquire(timeout=0.1)
    try:
        yield
    finally:
        global_job_semaphore.release()


def _frame_paths(frames_dir: str) -> list[str]:
    return sorted(
        path for path in glob.glob(os.path.join(frames_dir, "*"))
        if os.path.isfile(path)
    )


def _is_process_segfault(returncode: int) -> bool:
    return returncode in (-signal.SIGSEGV, 128 + signal.SIGSEGV)


def _is_invalid_tilesize_error(stderr: str) -> bool:
    return "invalid tilesize argument" in stderr.lower()


def _real_esrgan_model_args(real_esrgan_bin: str) -> list[str]:
    model_dir = os.path.join(os.path.dirname(real_esrgan_bin), "models")
    if os.path.isdir(model_dir):
        return ["-m", model_dir]
    return []


def _real_esrgan_python_runner_path() -> str:
    backend_dir = Path(__file__).resolve().parent.parent.parent
    return str(backend_dir / "scripts" / "realesrgan_python_runner.py")


def _real_esrgan_base_command(
    real_esrgan_bin: str,
    input_path: str,
    output_path: str,
) -> list[str]:
    return [
        real_esrgan_bin,
        "-i", input_path,
        "-o", output_path,
        "-n", "realesrgan-x4plus",
        "-s", "4",
        "-f", "png",
        *_real_esrgan_model_args(real_esrgan_bin),
    ]


def _run_real_esrgan_directory(
    job_id: str,
    real_esrgan_bin: str,
    frames_dir: str,
    enhanced_dir: str,
) -> ProcessRunResult:
    return _run_subprocess_result(
        job_id,
        _real_esrgan_base_command(real_esrgan_bin, frames_dir, enhanced_dir),
    )


def _run_real_esrgan_per_frame(
    job_id: str,
    real_esrgan_bin: str,
    frames_dir: str,
    enhanced_dir: str,
) -> None:
    frame_paths = _frame_paths(frames_dir)
    if not frame_paths:
        raise RuntimeError("No extracted frames available for Real-ESRGAN")

    os.makedirs(enhanced_dir, exist_ok=True)
    for frame_path in frame_paths:
        output_path = os.path.join(
            enhanced_dir,
            f"{Path(frame_path).stem}.png",
        )
        result = _run_subprocess_result(
            job_id,
            [
                *_real_esrgan_base_command(real_esrgan_bin, frame_path, output_path),
                "-j", "1:1:1",
            ],
        )
        if result.returncode != 0:
            raise _SubprocessError(result.returncode, result.stderr)


def _run_real_esrgan_python_backend(
    job_id: str,
    python_executable: str,
    model_path: str,
    frames_dir: str,
    enhanced_dir: str,
) -> None:
    backend_dir = str(Path(__file__).resolve().parent.parent.parent)
    result = _run_subprocess_result_isolated(
        job_id,
        [
            python_executable,
            _real_esrgan_python_runner_path(),
            "--input", frames_dir,
            "--output", enhanced_dir,
            "--model", model_path,
            "--tile", "0",
        ],
        cwd=backend_dir,
    )
    if result.returncode != 0:
        raise _SubprocessError(result.returncode, result.stderr)


def _run_real_esrgan_ncnn_with_frame_retry(
    job_id: str,
    real_esrgan_bin: str,
    frames_dir: str,
    enhanced_dir: str,
) -> None:
    result = _run_real_esrgan_directory(job_id, real_esrgan_bin, frames_dir, enhanced_dir)
    if result.returncode == 0:
        return

    last_error = _SubprocessError(result.returncode, result.stderr)
    if not (
        _is_process_segfault(result.returncode)
        or _is_invalid_tilesize_error(result.stderr)
    ):
        raise last_error

    logger.warning(
        "[%s] Real-ESRGAN directory mode failed; retrying frame-by-frame: %s",
        job_id,
        last_error,
    )
    shutil.rmtree(enhanced_dir, ignore_errors=True)
    os.makedirs(enhanced_dir, exist_ok=True)

    _run_real_esrgan_per_frame(job_id, real_esrgan_bin, frames_dir, enhanced_dir)


def _run_real_esrgan(
    job_id: str,
    *,
    backend: str,
    real_esrgan_bin: str | None,
    python_executable: str | None,
    model_path: str | None,
    frames_dir: str,
    enhanced_dir: str,
) -> None:
    if backend not in {"auto", "ncnn", "python"}:
        raise RuntimeError("REAL_ESRGAN_BACKEND must be one of: auto, ncnn, python")

    ncnn_error: Exception | None = None
    if backend in {"auto", "ncnn"} and real_esrgan_bin:
        try:
            _run_real_esrgan_ncnn_with_frame_retry(
                job_id,
                real_esrgan_bin,
                frames_dir,
                enhanced_dir,
            )
            return
        except _CancelledError:
            raise
        except Exception as error:
            ncnn_error = error
            if backend == "ncnn":
                raise
            logger.warning(
                "[%s] Real-ESRGAN ncnn backend failed; trying Python backend: %s",
                job_id,
                error,
            )
            shutil.rmtree(enhanced_dir, ignore_errors=True)
            os.makedirs(enhanced_dir, exist_ok=True)

    if backend in {"auto", "python"}:
        if not python_executable or not model_path:
            if ncnn_error:
                raise RuntimeError(_python_backend_install_message()) from ncnn_error
            raise RuntimeError(_real_esrgan_install_message())
        _run_real_esrgan_python_backend(
            job_id,
            python_executable,
            model_path,
            frames_dir,
            enhanced_dir,
        )
        return

    raise RuntimeError(_real_esrgan_install_message())


def _target_enhance_height(height: int) -> int:
    if height >= 2160:
        target_height = height
    else:
        target_height = min(max(720, height * 2), 2160)
    if target_height % 2 != 0:
        target_height += 1
    return target_height


def _process_chunk(
    *,
    job_id: str,
    chunk_index: int,
    chunk_path: str,
    tmp_base: str,
    ffmpeg_exe: str,
    fps: float,
    target_height: int,
    backend: str,
    real_esrgan_bin: str | None,
    real_esrgan_python: str | None,
    real_esrgan_model_path: str | None,
) -> tuple[int, str]:
    with _global_slot(job_id):
        _raise_if_cancelled(job_id)

        frames_dir = os.path.join(tmp_base, f"frames_{chunk_index:04d}")
        enhanced_dir = os.path.join(tmp_base, f"enhanced_{chunk_index:04d}")
        enhanced_chunk = os.path.join(tmp_base, f"enhanced_chunk_{chunk_index:04d}.mp4")

        try:
            os.makedirs(frames_dir)
            os.makedirs(enhanced_dir)

            _run_subprocess(job_id, [
                ffmpeg_exe, "-y", "-i", chunk_path,
                os.path.join(frames_dir, "%08d.png"),
            ])

            _run_real_esrgan(
                job_id,
                backend=backend,
                real_esrgan_bin=real_esrgan_bin,
                python_executable=real_esrgan_python,
                model_path=real_esrgan_model_path,
                frames_dir=frames_dir,
                enhanced_dir=enhanced_dir,
            )

            _run_subprocess(job_id, [
                ffmpeg_exe, "-y",
                "-framerate", str(fps),
                "-i", os.path.join(enhanced_dir, "%08d.png"),
                "-vf", f"scale=-2:{target_height}",
                "-c:v", "libx264",
                "-crf", "18",
                "-preset", "slow",
                "-pix_fmt", "yuv420p",
                enhanced_chunk,
            ])

            return chunk_index, enhanced_chunk

        finally:
            shutil.rmtree(frames_dir, ignore_errors=True)
            shutil.rmtree(enhanced_dir, ignore_errors=True)


def _process_job(job_id: str) -> None:
    job = _jobs[job_id]
    settings = get_settings()
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    tmp_base = os.path.join(settings.temp_storage_dir, f"{job_id}_work")
    chunks_dir = os.path.join(tmp_base, "chunks")
    out_filename = f"enhanced_{job_id}.mp4"
    out_path = os.path.join(settings.temp_storage_dir, out_filename)

    try:
        # 1. Backend check
        backend = settings.real_esrgan_backend
        real_esrgan_bin = _resolve_real_esrgan_bin(settings.real_esrgan_bin)
        real_esrgan_python = _resolve_real_esrgan_python(settings.real_esrgan_python)
        real_esrgan_model_path = _resolve_real_esrgan_model_path(settings.real_esrgan_model_path)
        if backend == "ncnn" and not real_esrgan_bin:
            with _lock:
                job.status = "failed"
                job.error = _real_esrgan_install_message()
            return
        if backend == "python" and (not real_esrgan_python or not real_esrgan_model_path):
            with _lock:
                job.status = "failed"
                job.error = _real_esrgan_install_message()
            return
        if backend == "auto" and not real_esrgan_bin and (not real_esrgan_python or not real_esrgan_model_path):
            with _lock:
                job.status = "failed"
                job.error = _real_esrgan_install_message()
            return

        # 2. Probe video metadata
        fps, duration, width, height = _probe_video(job.input_path)
        target_height = _target_enhance_height(height)

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
            with _global_slot(job_id):
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
        with _global_slot(job_id):
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

        # 7. Process chunks in parallel, preserving output order for concat.
        enhanced_chunks: list[str | None] = [None] * total_chunks
        completed_chunks = 0
        chunk_workers = min(MAX_CHUNK_WORKERS, total_chunks)
        logger.info(f"[{job_id}] Enhancing {total_chunks} chunks with {chunk_workers} workers")

        with _lock:
            if job.status == "cancelled":
                raise _CancelledError()
            job.phase = f"enhancing 0/{total_chunks}"
            job.progress = 5

        with ThreadPoolExecutor(max_workers=chunk_workers, thread_name_prefix=f"enhance-{job_id[:8]}") as executor:
            futures = [
                executor.submit(
                    _process_chunk,
                    job_id=job_id,
                    chunk_index=idx,
                    chunk_path=chunk_path,
                    tmp_base=tmp_base,
                    ffmpeg_exe=ffmpeg_exe,
                    fps=fps,
                    target_height=target_height,
                    backend=backend,
                    real_esrgan_bin=real_esrgan_bin,
                    real_esrgan_python=real_esrgan_python,
                    real_esrgan_model_path=real_esrgan_model_path,
                )
                for idx, chunk_path in enumerate(chunk_files)
            ]

            try:
                for future in as_completed(futures):
                    idx, enhanced_chunk = future.result()
                    enhanced_chunks[idx] = enhanced_chunk
                    completed_chunks += 1
                    with _lock:
                        if job.status == "cancelled":
                            raise _CancelledError()
                        job.phase = f"enhancing {completed_chunks}/{total_chunks}"
                        job.progress = 5 + int((completed_chunks / total_chunks) * 85)
            except _CancelledError:
                for future in futures:
                    future.cancel()
                _kill_job_processes(job)
                raise
            except Exception:
                for future in futures:
                    future.cancel()
                _kill_job_processes(job)
                raise

        if any(chunk is None for chunk in enhanced_chunks):
            raise RuntimeError("One or more chunks did not finish enhancing")
        ordered_enhanced_chunks = [chunk for chunk in enhanced_chunks if chunk is not None]

        # 8. Assemble all chunks + mux audio
        with _lock:
            job.phase = "assembling"
            job.progress = 92

        concat_list = os.path.join(tmp_base, "concat_list.txt")
        with open(concat_list, "w") as f:
            for c in ordered_enhanced_chunks:
                f.write(f"file '{c}'\n")

        if has_audio:
            with _global_slot(job_id):
                _run_subprocess(job_id, [
                    ffmpeg_exe, "-y",
                    "-f", "concat", "-safe", "0", "-i", concat_list,
                    "-i", audio_path,
                    "-map", "0:v", "-map", "1:a",
                    "-c:v", "copy", "-c:a", "copy",
                    out_path,
                ])
        else:
            with _global_slot(job_id):
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
        cleanup_paths([out_path])

    except Exception as e:
        logger.error(f"[{job_id}] Process error: {e}", exc_info=True)
        with _lock:
            if job.status == "processing":
                job.status = "failed"
                job.error = str(e)

    finally:
        shutil.rmtree(tmp_base, ignore_errors=True)
        cleanup_paths([job.input_path])
