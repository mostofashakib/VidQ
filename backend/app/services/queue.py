"""
Async video recording queue.

Jobs are enqueued and processed by per-job background threads capped at five
active downloads. Overflow jobs remain queued until an active thread exits.
The API returns immediately with a job_id so the client can poll
/queue/{job_id} for status, or DELETE it to cancel.
"""
import asyncio
import threading
import time
import uuid
import logging
from urllib.parse import urlparse, unquote
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("VideoQueue")

MAX_DOWNLOAD_WORKERS = 5


def _fallback_title(url: str) -> str:
    """Create a stable title when LLM metadata extraction is unavailable."""
    parsed = urlparse(url)
    path_name = unquote(parsed.path.rstrip("/").split("/")[-1])
    if path_name:
        base_name = path_name.rsplit(".", 1)[0]
        cleaned = base_name.replace("_", " ").replace("-", " ").strip()
        if cleaned:
            return cleaned[:120]
    if parsed.netloc:
        return parsed.netloc
    return url[:120] or "Video"


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class RecordingJob:
    job_id: str
    url: str
    category: str
    token: str
    status: JobStatus = JobStatus.QUEUED
    result: Optional[dict] = None
    error: Optional[str] = None
    phase: Optional[str] = None               # current pipeline phase
    started_at: Optional[float] = None        # time.time() when processing began
    recording_started_at: Optional[float] = None  # time.time() when MediaRecorder fired
    download_progress: int = 0                # 0-100, ffmpeg download percentage
    recording_duration: Optional[int] = None  # progress target; detected video duration or recording cap
    # Per-job cancellation flag checked during the recording sleep
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)


class VideoQueue:
    """Thread-safe queue backed by capped per-job daemon threads."""

    def __init__(self, max_workers: int = MAX_DOWNLOAD_WORKERS):
        self._jobs: dict[str, RecordingJob] = {}
        self._queue: list[str] = []
        self._max_workers = max(1, max_workers)
        self._active_count = 0
        self._thread_counter = 0
        self._lock = threading.RLock()
        logger.info(
            f"VideoQueue initialized with capped per-job threads "
            f"(max active downloads: {self._max_workers})."
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, url: str, category: str, token: str) -> RecordingJob:
        job = RecordingJob(job_id=uuid.uuid4().hex, url=url, category=category, token=token)
        with self._lock:
            self._jobs[job.job_id] = job
            self._queue.append(job.job_id)
            queue_position = self._queue.index(job.job_id)
            self._start_available_jobs_locked()
        logger.info(
            f"Enqueued job {job.job_id} for URL: {url} "
            f"(pending position: {queue_position})"
        )
        return job

    def get(self, job_id: str) -> Optional[RecordingJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def position(self, job_id: str) -> int:
        """Return 0-indexed queue position. -1 if not pending."""
        with self._lock:
            try:
                return self._queue.index(job_id)
            except ValueError:
                return -1

    def cancel(self, job_id: str) -> bool:
        """
        Cancel a job.
        - QUEUED jobs are removed from the queue immediately.
        - PROCESSING jobs receive a cancel_event signal; the recording
          loop will stop at the next 1-second tick.
        Returns True if the job existed and was cancellable.
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if not job:
                return False
            if job.status == JobStatus.QUEUED:
                if job_id in self._queue:
                    self._queue.remove(job_id)
                job.status = JobStatus.CANCELLED
                logger.info(f"Job {job_id} cancelled while queued.")
                return True
            if job.status == JobStatus.PROCESSING:
                job.cancel_event.set()
                logger.info(f"Cancel signal sent to processing job {job_id}.")
                return True
            return False

    # ------------------------------------------------------------------
    # Worker
    # ------------------------------------------------------------------

    def _start_available_jobs_locked(self) -> None:
        """
        Start one independent daemon thread per pending job until the cap is hit.
        Caller must hold self._lock.
        """
        while self._active_count < self._max_workers and self._queue:
            job_id = self._queue.pop(0)
            job = self._jobs.get(job_id)
            if not job:
                logger.warning(f"Missing queued job {job_id}, skipping.")
                continue
            if job.status == JobStatus.CANCELLED:
                logger.info(f"Skipping cancelled queued job {job_id}.")
                continue

            self._active_count += 1
            self._thread_counter += 1
            thread_index = self._thread_counter
            thread = threading.Thread(
                target=self._thread_entry,
                args=(job, thread_index),
                name=f"video-download-job-{job.job_id[:8]}",
                daemon=True,
            )
            logger.info(
                f"Starting isolated download thread {thread.name} "
                f"({self._active_count}/{self._max_workers} active)."
            )
            thread.start()

    def _thread_entry(self, job: RecordingJob, thread_index: int) -> None:
        try:
            self._process(job, thread_index=thread_index)
        finally:
            with self._lock:
                self._active_count = max(0, self._active_count - 1)
                logger.info(
                    f"Download thread finished for job {job.job_id}; "
                    f"{self._active_count}/{self._max_workers} active."
                )
                self._start_available_jobs_locked()

    def _process(self, job: RecordingJob, thread_index: int = 0):
        if job.status == JobStatus.CANCELLED:
            logger.info(f"Skipping cancelled job {job.job_id}.")
            return

        logger.info(
            f"Thread {thread_index} processing isolated download job "
            f"{job.job_id} — {job.url}"
        )
        with self._lock:
            if job.status == JobStatus.CANCELLED:
                logger.info(f"Skipping cancelled job {job.job_id}.")
                return
            job.status = JobStatus.PROCESSING
            job.started_at = time.time()

        def _set_phase(phase: str, recording_duration: int | None = None) -> None:
            with self._lock:
                job.phase = phase
                if phase == "heavy_pass_recording":
                    job.recording_started_at = time.time()
                    if recording_duration is not None:
                        job.recording_duration = recording_duration

        def _on_progress(pct: int) -> None:
            with self._lock:
                job.download_progress = pct

        async def _actual_work():
            from app.services.scraper import run_extraction, USER_AGENTS
            from app.state import llm_manager
            from app.routers.video import call_llm_with_html_and_screenshot
            from bs4 import BeautifulSoup
            import random

            user_agent = random.choice(USER_AGENTS)
            html, screenshot_b64, network_video_urls, thumbnail_url, temp_video_url = await run_extraction(
                url=job.url,
                user_agent=user_agent,
                llm_manager=llm_manager,
                max_record_seconds=10800,
                cancel_event=job.cancel_event,
                phase_callback=_set_phase,
                progress_callback=_on_progress,
            )

            if not screenshot_b64:
                # Embed fast-path: extract metadata from HTML directly, skip LLM
                embed_soup = BeautifulSoup(html, "html.parser") if html else None
                title = None
                if embed_soup:
                    og_title = embed_soup.find("meta", property="og:title", content=True)
                    title = og_title["content"] if og_title else (
                        embed_soup.title.text.strip() if embed_soup.title else None
                    )
                result = {"title": title or job.url, "thumbnail": thumbnail_url, "video_url": temp_video_url or ""}
            else:
                try:
                    result = await call_llm_with_html_and_screenshot(
                        llm_manager, html, screenshot_b64, network_video_urls, thumbnail_url
                    )
                except Exception as metadata_error:
                    logger.warning(
                        f"Job {job.job_id}: LLM metadata extraction failed; "
                        f"using fallback metadata. Error: {metadata_error}"
                    )
                    result = {
                        "title": _fallback_title(job.url),
                        "description": "",
                        "duration": None,
                        "thumbnail": thumbnail_url,
                        "video_url": temp_video_url or (network_video_urls[0] if network_video_urls else job.url),
                    }
                result["thumbnail"] = result.get("thumbnail") or thumbnail_url
                if temp_video_url:
                    result["video_url"] = temp_video_url
            return result, temp_video_url

        async def _run_with_cancel_watcher():
            """
            Runs _actual_work as an asyncio Task while a watcher task polls
            cancel_event every 300 ms and cancels the main task if it fires.
            This makes cancel_event effective at ANY await point inside the job
            (LLM calls, Playwright navigation, recording sleep, etc.).
            """
            main_task = asyncio.create_task(_actual_work())

            async def _watch():
                while not main_task.done():
                    if job.cancel_event.is_set():
                        main_task.cancel()
                        return
                    await asyncio.sleep(0.3)

            watch_task = asyncio.create_task(_watch())
            try:
                return await main_task
            finally:
                watch_task.cancel()
                try:
                    await watch_task
                except (asyncio.CancelledError, Exception):
                    pass

        try:
            result, temp_video_url = asyncio.run(_run_with_cancel_watcher())

            if job.cancel_event.is_set():
                with self._lock:
                    job.status = JobStatus.CANCELLED
                logger.info(f"Job {job.job_id} marked cancelled after processing.")
                return

            from app.db import SessionLocal, Video
            db = SessionLocal()
            try:
                def _video_payload(video: Video) -> dict:
                    return {
                        "id": video.id,
                        "url": video.url,
                        "category": video.category,
                        "title": video.title,
                        "duration": video.duration,
                        "thumbnail": video.thumbnail,
                        "source": video.source,
                        "created_at": video.created_at.isoformat() if video.created_at else None,
                    }

                video_url = result.get("video_url") or job.url
                title = result.get("title")
                duration = result.get("duration")
                thumbnail = result.get("thumbnail")
                existing = db.query(Video).filter(Video.url == video_url.strip().lower()).first()
                if not existing and title and title.strip() not in ["", "Untitled Video", "Video"]:
                    existing = db.query(Video).filter(Video.title == title.strip()).first()
                if not existing:
                    v = Video(
                        url=video_url.strip().lower(),
                        category=job.category,
                        title=title,
                        duration=duration,
                        thumbnail=thumbnail,
                    )
                    db.add(v)
                    db.commit()
                    db.refresh(v)
                    result["db_id"] = v.id
                    result["video"] = _video_payload(v)
                    logger.info(f"Job {job.job_id} saved video id={v.id}")
                else:
                    result["db_id"] = existing.id
                    result["video"] = _video_payload(existing)
                    logger.info(f"Job {job.job_id} — video already exists.")
            finally:
                db.close()

            with self._lock:
                job.result = result
                job.status = JobStatus.DONE
            logger.info(f"Job {job.job_id} completed successfully.")

        except asyncio.CancelledError:
            with self._lock:
                job.status = JobStatus.CANCELLED
            logger.info(f"Job {job.job_id} cancelled.")
        except Exception as e:
            logger.error(f"Job {job.job_id} failed: {e}", exc_info=True)
            with self._lock:
                job.status = JobStatus.FAILED
                job.error = str(e)


# Module-level singleton
video_queue = VideoQueue()
