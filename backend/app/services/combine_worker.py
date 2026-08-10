import os
import queue
import subprocess
import threading
import logging
from typing import Optional

import imageio_ffmpeg

from app.config import get_settings
from app.services.ffmpeg_utils import (
    TARGET_FPS,
    build_normalize_command,
    output_file_is_valid,
    probe_duration,
    probe_missing_audio,
    run_progress_process,
    scale_to_target_chain,
)
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

# Share of overall_progress spent on the per-clip normalize prepass.
NORMALIZE_PROGRESS_SHARE = 40

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


def _combine_fade_duration(durations: list[float], preferred: float = 0.5) -> float:
    positive_durations = [duration for duration in durations if duration > 0]
    if not positive_durations:
        return preferred
    shortest = min(positive_durations)
    return max(0.05, min(preferred, shortest / 3))


def _scale_to_720p_filter(input_label: str, output_label: str) -> str:
    # fps has to come last: xfade rejects its inputs unless the frame rate is constant
    # at the point it sees them, and setpts alone leaves the rate undefined.
    chain = scale_to_target_chain(fps=None)
    return f"{input_label}{chain},settb=AVTB,setpts=PTS-STARTPTS,fps={TARGET_FPS}{output_label}"


def _build_xfade_filter(durations: list[float], fade_duration: float = 0.5) -> tuple[str, str]:
    """Return (video_filter, audio_filter) strings for N clips with xfade transitions."""
    n = len(durations)
    normalized_video_parts = [
        _scale_to_720p_filter(f"[{i}:v]", f"[v{i}]")
        for i in range(n)
    ]
    normalized_audio_parts = [
        (
            f"[{i}:a]"
            "aformat=sample_rates=48000:channel_layouts=stereo,"
            "asetpts=PTS-STARTPTS"
            f"[a{i}]"
        )
        for i in range(n)
    ]

    if n == 1:
        return (
            ";".join([*normalized_video_parts, "[v0]null[vout]"]),
            ";".join([*normalized_audio_parts, "[a0]anull[aout]"]),
        )

    video_parts = [*normalized_video_parts]
    audio_parts = [*normalized_audio_parts]
    cumulative = 0.0

    for i in range(n - 1):
        offset = cumulative + durations[i] - fade_duration * (i + 1)
        cumulative += durations[i]

        if i == 0:
            v_in = "[v0][v1]"
            a_in = "[a0][a1]"
        else:
            v_in = f"[vx{i}][v{i+1}]"
            a_in = f"[ax{i}][a{i+1}]"

        v_out = "[vout]" if i == n - 2 else f"[vx{i+1}]"
        a_out = "[aout]" if i == n - 2 else f"[ax{i+1}]"

        video_parts.append(
            f"{v_in}xfade=transition=fade:duration={fade_duration}:offset={offset:.3f}{v_out}"
        )
        audio_parts.append(
            f"{a_in}acrossfade=d={fade_duration}{a_out}"
        )

    return ";".join(video_parts), ";".join(audio_parts)


def _normalize_clip(
    job: CombineJob,
    source_path: str,
    out_path: str,
    *,
    silent_audio: bool,
    duration: float,
    progress_base: float,
    progress_span: float,
) -> Optional[str]:
    """Re-encode one clip to the canonical format, the same conversion the upload path
    runs. Returns the normalized path, or None if it was cancelled or ffmpeg failed."""
    cmd = build_normalize_command(
        input_path=source_path,
        output_path=out_path,
        fps=TARGET_FPS,
        crf=14,
        preset="veryfast",
        audio_bitrate="320k",
        silent_audio=silent_audio,
        fixed_duration=duration or None,
    )

    def update_progress(current_s: float) -> None:
        if duration <= 0:
            return
        with _lock:
            job.overall_progress = min(
                NORMALIZE_PROGRESS_SHARE,
                int(progress_base + min(1.0, current_s / duration) * progress_span),
            )

    result = run_progress_process(
        cmd=cmd,
        job=job,
        lock=_lock,
        popen=subprocess.Popen,
        on_progress=update_progress,
    )

    if result.cancelled:
        return None

    if result.returncode != 0 or not output_file_is_valid(out_path):
        logger.error(f"[{job.job_id}] Clip normalize failed: {result.stderr[-400:]}")
        cleanup_paths([out_path])
        return None

    return out_path


def _normalize_clips(
    job: CombineJob,
    file_paths: list[str],
    filenames: list[str],
    temp_paths: list[str],
) -> list[str]:
    """Re-encode every clip to the canonical format before it reaches the xfade graph,
    the same conversion the upload path runs. Returns the paths to concatenate.

    This is unconditional on purpose. A clip can change resolution, pixel format or
    frame rate part way through and still advertise clean, matching parameters in its
    header, and ffmpeg reacts to the change by reconfiguring the filter graph mid-run.
    That either stalls xfade ("N buffers queued in out_#0:0" → best_input assertion) or
    silently drops every frame after the change, and no probe of the source reveals it
    beforehand. Re-encoding each clip first collapses it to one stable configuration.
    """
    total = len(file_paths)
    prepared = list(file_paths)
    with _lock:
        job.phase = "normalizing"
        job.overall_progress = 0

    for i, path in enumerate(file_paths):
        if job.status == "cancelled":
            return prepared

        with _lock:
            job.clip_index = i + 1
        progress_base = i / total * NORMALIZE_PROGRESS_SHARE
        progress_span = NORMALIZE_PROGRESS_SHARE / total

        logger.info(f"[{job.job_id}] Normalizing clip {i+1}/{total}: {filenames[i]}")
        result_path = _normalize_clip(
            job,
            path,
            f"{os.path.splitext(path)[0]}_norm.mp4",
            silent_audio=probe_missing_audio(path),
            duration=probe_duration(path) or 0.0,
            progress_base=progress_base,
            progress_span=progress_span,
        )

        if result_path is None:
            if job.status == "cancelled":
                return prepared
            # Fall back to the source clip; the in-graph scale/pad filters may still cope.
            logger.warning(
                f"[{job.job_id}] Could not normalize {filenames[i]} — using the source clip"
            )
        else:
            prepared[i] = result_path
            temp_paths.append(result_path)

        with _lock:
            job.overall_progress = int(progress_base + progress_span)

    return prepared


def _run_concat(job: CombineJob, paths: list[str], out_path: str) -> str:
    """Run the xfade concat pass. Returns "done", "cancelled" or "failed"."""
    durations = [probe_duration(path) or 5.0 for path in paths]
    fade_duration = _combine_fade_duration(durations)
    total_duration = sum(durations) - (len(durations) - 1) * fade_duration

    with _lock:
        job.phase = "concatenating"
        job.overall_progress = NORMALIZE_PROGRESS_SHARE

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [ffmpeg_exe, "-y"]
    for path in paths:
        cmd.extend(["-i", path])

    if len(paths) == 1:
        filter_args = [
            "-filter_complex", _scale_to_720p_filter("[0:v]", "[vout]"),
            "-map", "[vout]", "-map", "0:a?",
        ]
    else:
        v_filter, a_filter = _build_xfade_filter(durations, fade_duration=fade_duration)
        filter_args = [
            "-filter_complex", f"{v_filter};{a_filter}",
            "-map", "[vout]", "-map", "[aout]",
        ]

    cmd.extend([
        *filter_args,
        "-c:v", "libx264", "-crf", "14", "-preset", "slow",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "320k", "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        out_path,
    ])

    logger.info(f"[{job.job_id}] Running ffmpeg concat ({len(paths)} clips) at 1280x720")

    def update_progress(current_s: float) -> None:
        if total_duration <= 0:
            return
        with _lock:
            job.overall_progress = min(
                99,
                int(NORMALIZE_PROGRESS_SHARE + current_s / total_duration * (99 - NORMALIZE_PROGRESS_SHARE)),
            )

    result = run_progress_process(
        cmd=cmd,
        job=job,
        lock=_lock,
        popen=subprocess.Popen,
        on_progress=update_progress,
    )

    if result.cancelled:
        cleanup_paths([out_path])
        return "cancelled"

    if result.returncode != 0 or not output_file_is_valid(out_path):
        logger.error(f"[{job.job_id}] ffmpeg failed: {result.stderr[-400:]}")
        cleanup_paths([out_path])
        return "failed"

    return "done"


def _process_job(job_id: str, file_paths: list[str], filenames: list[str]) -> None:
    job = _jobs[job_id]
    settings = get_settings()

    temp_paths: list[str] = []

    try:
        # Phase 1: re-encode every clip to the canonical format.
        prepared = _normalize_clips(job, file_paths, filenames, temp_paths)
        if job.status == "cancelled":
            return

        out_filename = f"combined_{job_id}.mp4"
        out_path = os.path.join(settings.temp_storage_dir, out_filename)

        # Phase 2: probe durations for the xfade offsets and run the concat.
        outcome = _run_concat(job, prepared, out_path)

        if outcome == "cancelled":
            return

        if outcome == "failed":
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
        cleanup_paths(file_paths + temp_paths)
