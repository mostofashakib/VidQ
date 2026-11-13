import asyncio
import os
import queue
import re
import subprocess
import threading
import logging
from typing import Optional

import imageio_ffmpeg

from app.config import get_settings
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

logger = logging.getLogger("TranslateWorker")

MAX_WORKERS = 3  # Whisper + LLM are heavy; keep pool smaller
AUDIO_EXTRACT_TIMEOUT_SECS = 120  # ffmpeg audio strip for a 2h file is ~30s; 120s is a safe ceiling

_jobs: dict[str, "TranslateJob"] = {}
_lock = threading.Lock()
_task_queue: queue.Queue = queue.Queue()

_pool_state = WorkerPoolState()

# Approximate chars per token; used for chunking
_CHARS_PER_TOKEN = 4
_CHUNK_TOKEN_BUDGET = 2000
_LINE_NUMBER_TOKENS = 3  # "12| " before each segment's text
_NUMBERED_LINE_RE = re.compile(r"^\s*(\d+)\s*\|\s?(.*)$")


class TranslateJob:
    def __init__(self, job_id: str, filename: str, save_to_db: bool = False, cleanup_input: bool = True):
        self.job_id = job_id
        self.filename = filename
        self.save_to_db = save_to_db
        self.cleanup_input = cleanup_input
        self.status = "queued"  # queued | processing | done | failed | cancelled
        self.error: Optional[str] = None
        self.phase = "queued"  # queued | extracting_audio | transcribing | translating | burning
        self.overall_progress: int = 0
        self.chunk_index: int = 0
        self.total_chunks: int = 0
        self.result_url: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None


def _ensure_pool() -> None:
    ensure_worker_pool(
        _pool_state,
        max_workers=MAX_WORKERS,
        target=_worker_loop,
        name_prefix="translate-worker",
        logger=logger,
        label="Translate",
    )


def _worker_loop() -> None:
    while True:
        job_id, file_path, original_name = _task_queue.get()
        try:
            process_queued_job(
                job_id=job_id,
                jobs=_jobs,
                lock=_lock,
                logger=logger,
                cleanup_cancelled=lambda job: cleanup_paths([file_path]) if (job and getattr(job, "cleanup_input", True)) else None,
                picked_message=lambda job: f"[{job.job_id}] Worker picked up {original_name}",
                process=lambda job: _process_job(job.job_id, file_path, original_name),
            )
        except Exception as e:
            logger.error(f"Translate worker loop error for {job_id}: {e}", exc_info=True)
        finally:
            _task_queue.task_done()


def get_job(job_id: str) -> Optional[TranslateJob]:
    return get_registered_job(_jobs, _lock, job_id)


def cancel_job(job_id: str) -> bool:
    return cancel_registered_job(_jobs, _lock, job_id)


def start_translate_job(file_path: str, original_name: str, save_to_db: bool = False, cleanup_input: bool = True) -> str:
    _ensure_pool()
    job_id = new_job_id()
    job = TranslateJob(job_id=job_id, filename=original_name, save_to_db=save_to_db, cleanup_input=cleanup_input)
    enqueue_registered_job(_jobs, _lock, _task_queue, job, (job_id, file_path, original_name))
    logger.info(f"[{job_id}] Queued: {original_name}")
    return job_id


def _extract_audio(job: TranslateJob, video_path: str, audio_path: str) -> bool:
    """Extract mono 16kHz WAV for Whisper. Returns True on success."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe, "-y", "-i", video_path,
        "-vn", "-ar", "16000", "-ac", "1",
        "-c:a", "pcm_s16le",
        audio_path,
    ]
    logger.info(f"[{job.job_id}] Extracting audio → {os.path.basename(audio_path)}")
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=AUDIO_EXTRACT_TIMEOUT_SECS)
    if result.returncode != 0 or not os.path.exists(audio_path):
        logger.error(f"[{job.job_id}] Audio extraction failed: {result.stderr[-300:]}")
        return False
    return True


def _segments_to_srt(segments: list[dict]) -> str:
    """Convert Whisper verbose_json segments to SRT text."""
    lines = []
    for i, seg in enumerate(segments, 1):
        start = _format_srt_time(seg["start"])
        end = _format_srt_time(seg["end"])
        text = seg["text"].strip()
        lines.append(f"{i}\n{start} --> {end}\n{text}\n")
    return "\n".join(lines)


def _format_srt_time(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    ms = int((seconds - int(seconds)) * 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _chunk_segments(segments: list[dict]) -> list[list[dict]]:
    """Group segments into chunks capped at _CHUNK_TOKEN_BUDGET tokens."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    current_tokens = 0
    for seg in segments:
        seg_tokens = len(seg.get("text", "")) // _CHARS_PER_TOKEN + _LINE_NUMBER_TOKENS
        if current and current_tokens + seg_tokens > _CHUNK_TOKEN_BUDGET:
            chunks.append(current)
            current = []
            current_tokens = 0
        current.append(seg)
        current_tokens += seg_tokens
    if current:
        chunks.append(current)
    return chunks if chunks else [[]]


def _build_translation_prompt(segments: list[dict]) -> str:
    """
    The chunk's text as numbered lines. Timings stay out: the model would
    only have to copy them back, and copying is most of its output.
    """
    numbered = "\n".join(f"{i}| {seg['text'].strip()}" for i, seg in enumerate(segments, 1))
    return f"""You are a professional subtitle translator.

Translate each numbered subtitle line below to English exactly as spoken.
Do NOT summarize, condense, merge or omit any line.
Do NOT add explanations or commentary.
Return ONLY the translated lines, each with its number, in the same "N| text" format.

{numbered}"""


def _numbered_lines(text: str) -> dict[int, str]:
    """The model's 'N| text' answers by line number, ignoring anything else it wrote."""
    lines: dict[int, str] = {}
    for line in text.splitlines():
        match = _NUMBERED_LINE_RE.match(line)
        if match and match.group(2).strip():
            lines.setdefault(int(match.group(1)), match.group(2).strip())
    return lines


def _merge_translated_chunks(chunks: list[list[dict]], translated_chunks: list[str]) -> str:
    """
    One SRT from every chunk's answer, keeping the original timings. A line
    the model skipped keeps its original text.
    """
    merged: list[dict] = []
    missing = 0
    for segments, answer in zip(chunks, translated_chunks):
        lines = _numbered_lines(answer)
        for i, seg in enumerate(segments, 1):
            missing += i not in lines
            merged.append({**seg, "text": lines.get(i, seg["text"])})
    if missing:
        logger.warning(f"Translation skipped {missing} of {len(merged)} lines; they keep their original text")
    return _segments_to_srt(merged)


def _burn_subtitles(
    job: TranslateJob,
    video_path: str,
    srt_path: str,
    out_path: str,
    total_duration: Optional[float],
) -> bool:
    """Burn subtitles into video at 720p. Returns True on success."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()

    # Escape srt_path for ffmpeg subtitles filter (Windows backslashes + colons need escaping)
    escaped_srt = srt_path.replace("\\", "/").replace(":", "\\:")

    subtitle_style = (
        "Alignment=2,MarginV=30,FontName=Arial,FontSize=20,"
        "PrimaryColour=&Hffffff,OutlineColour=&H000000,Outline=2,Shadow=1"
    )
    vf = f"scale=-2:720:flags=lanczos,subtitles='{escaped_srt}':force_style='{subtitle_style}'"

    cmd = [
        ffmpeg_exe, "-y", "-i", video_path,
        "-vf", vf,
        "-c:v", "libx264", "-crf", "18", "-preset", "slow",
        "-c:a", "copy",
        "-movflags", "+faststart",
        "-progress", "pipe:1", "-nostats",
        out_path,
    ]

    logger.info(f"[{job.job_id}] Burning subtitles → {os.path.basename(out_path)}")

    def update_progress(current_s: float) -> None:
        if not total_duration or total_duration <= 0:
            return
        with _lock:
            job.overall_progress = min(99, int(80 + current_s / total_duration * 19))

    result = run_progress_process(
        cmd=cmd,
        job=job,
        lock=_lock,
        popen=subprocess.Popen,
        on_progress=update_progress,
    )

    if result.cancelled:
        cleanup_paths([out_path])
        return False

    if result.returncode != 0 or not output_file_is_valid(out_path):
        logger.error(f"[{job.job_id}] Subtitle burn failed: {result.stderr[-400:]}")
        return False

    return True


def _process_job(job_id: str, file_path: str, original_name: str) -> None:
    from app.state import transcription_adapter, translate_llm_manager

    job = _jobs[job_id]
    settings = get_settings()

    audio_path = os.path.join(settings.temp_storage_dir, f"audio_{job_id}.wav")
    srt_path = os.path.join(settings.temp_storage_dir, f"subs_{job_id}.srt")
    out_filename = f"translated_{job_id}.mp4"
    out_path = os.path.join(settings.temp_storage_dir, out_filename)

    try:
        # Phase 1: Extract audio
        with _lock:
            job.phase = "extracting_audio"
            job.overall_progress = 0

        ok = _extract_audio(job, file_path, audio_path)
        if not ok:
            with _lock:
                job.status = "failed"
                job.error = "Audio extraction failed"
            return

        if job.status == "cancelled":
            return

        with _lock:
            job.overall_progress = 5

        # Phase 2: Transcription (adapter-driven: openai_whisper | local_whisper)
        with _lock:
            job.phase = "transcribing"
            job.overall_progress = 10

        logger.info(f"[{job_id}] Transcribing via {transcription_adapter.__class__.__name__}")
        try:
            segments = transcription_adapter.transcribe(audio_path)
        except Exception as e:
            logger.error(f"[{job_id}] Transcription failed: {e}")
            with _lock:
                job.status = "failed"
                job.error = f"Transcription failed: {e}"
            return

        if job.status == "cancelled":
            return

        with _lock:
            job.overall_progress = 30

        # Phase 3: LLM translation (chunked)
        with _lock:
            job.phase = "translating"

        chunks = _chunk_segments(segments)
        total_chunks = len(chunks)
        with _lock:
            job.total_chunks = total_chunks

        logger.info(f"[{job_id}] Translating {len(segments)} segments in {total_chunks} chunks")

        translated_chunks: list[str] = []
        for i, chunk in enumerate(chunks):
            if job.status == "cancelled":
                return

            with _lock:
                job.chunk_index = i + 1
                job.overall_progress = 30 + int((i / total_chunks) * 50)

            prompt = _build_translation_prompt(chunk)
            try:
                result = asyncio.run(translate_llm_manager.execute_translate(prompt))
                translated_chunks.append(result)
            except Exception as e:
                logger.error(f"[{job_id}] Translation chunk {i+1} failed: {e}")
                with _lock:
                    job.status = "failed"
                    job.error = f"Translation failed: {e}"
                return

        final_srt = _merge_translated_chunks(chunks, translated_chunks)
        with open(srt_path, "w", encoding="utf-8") as f:
            f.write(final_srt)

        with _lock:
            job.overall_progress = 80

        if job.status == "cancelled":
            return

        # Phase 4: Burn subtitles
        with _lock:
            job.phase = "burning"

        video_duration = probe_duration(file_path)
        ok = _burn_subtitles(job, file_path, srt_path, out_path, video_duration)
        if not ok and job.status != "cancelled":
            with _lock:
                job.status = "failed"
                job.error = "Subtitle burning failed"
            return

        if job.status == "cancelled":
            return

        result_url = f"{settings.base_url}/temp_storage/{out_filename}"
        with _lock:
            job.overall_progress = 100
            job.status = "done"
            job.result_url = result_url
        logger.info(f"[{job_id}] Done: {out_filename}")

        if getattr(job, "save_to_db", False):
            try:
                from app.db import SessionLocal, Video
                with SessionLocal() as db:
                    v = Video(
                        url=result_url.strip().lower(),
                        category="translated",
                        title=f"{job.filename} (Subtitled)",
                        duration=video_duration,
                        source="translate",
                    )
                    db.add(v)
                    db.commit()
            except Exception as exc:
                logger.warning(f"Could not save translated video to DB: {exc}")

    except Exception as e:
        logger.error(f"[{job_id}] Process error: {e}", exc_info=True)
        with _lock:
            if job.status == "processing":
                job.status = "failed"
                job.error = str(e)
    finally:
        cleanup_paths([audio_path, srt_path])
        if getattr(job, "cleanup_input", True):
            cleanup_paths([file_path])
