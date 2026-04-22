"""
Async video recording queue.

Jobs are enqueued and processed in a background worker thread.
The API returns immediately with a job_id so the client can poll
/queue/{job_id} for status, or DELETE it to cancel.
"""
import asyncio
import threading
import uuid
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("VideoQueue")


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
    # Per-job cancellation flag checked during the recording sleep
    cancel_event: threading.Event = field(default_factory=threading.Event, repr=False, compare=False)


class VideoQueue:
    """Thread-safe singleton queue backed by a daemon worker thread."""

    def __init__(self):
        self._jobs: dict[str, RecordingJob] = {}
        self._queue: list[str] = []
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        logger.info("VideoQueue worker thread started.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, url: str, category: str, token: str) -> RecordingJob:
        job = RecordingJob(job_id=uuid.uuid4().hex, url=url, category=category, token=token)
        with self._lock:
            self._jobs[job.job_id] = job
            self._queue.append(job.job_id)
        self._event.set()
        logger.info(f"Enqueued job {job.job_id} for URL: {url}")
        return job

    def get(self, job_id: str) -> Optional[RecordingJob]:
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
        job = self._jobs.get(job_id)
        if not job:
            return False
        if job.status == JobStatus.QUEUED:
            with self._lock:
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

    def _run(self):
        while True:
            self._event.wait()
            self._event.clear()
            while True:
                with self._lock:
                    if not self._queue:
                        break
                    job_id = self._queue[0]
                job = self._jobs[job_id]
                self._process(job)
                with self._lock:
                    if self._queue and self._queue[0] == job_id:
                        self._queue.pop(0)

    def _process(self, job: RecordingJob):
        if job.status == JobStatus.CANCELLED:
            logger.info(f"Skipping cancelled job {job.job_id}.")
            return

        logger.info(f"Processing job {job.job_id} — {job.url}")
        job.status = JobStatus.PROCESSING

        async def _actual_work():
            from app.services.scraper import run_extraction, USER_AGENTS
            from app.state import llm_manager
            from app.routers.video import call_llm_with_html_and_screenshot
            import random

            user_agent = random.choice(USER_AGENTS)
            html, screenshot_b64, network_video_urls, thumbnail_url, temp_video_url = await run_extraction(
                url=job.url,
                user_agent=user_agent,
                llm_manager=llm_manager,
                max_record_seconds=10800,
                cancel_event=job.cancel_event,
            )
            result = await call_llm_with_html_and_screenshot(
                llm_manager, html, screenshot_b64, network_video_urls, thumbnail_url
            )
            result["thumbnail"] = result.get("thumbnail") or thumbnail_url
            # Always prefer the locally downloaded file over an expiring CDN URL
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
                job.status = JobStatus.CANCELLED
                logger.info(f"Job {job.job_id} marked cancelled after processing.")
                return

            from app.db import SessionLocal, Video
            db = SessionLocal()
            try:
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
                    logger.info(f"Job {job.job_id} saved video id={v.id}")
                else:
                    logger.info(f"Job {job.job_id} — video already exists.")
            finally:
                db.close()

            job.result = result
            job.status = JobStatus.DONE
            logger.info(f"Job {job.job_id} completed successfully.")

        except asyncio.CancelledError:
            job.status = JobStatus.CANCELLED
            logger.info(f"Job {job.job_id} cancelled.")
        except Exception as e:
            logger.error(f"Job {job.job_id} failed: {e}", exc_info=True)
            job.status = JobStatus.FAILED
            job.error = str(e)


# Module-level singleton
video_queue = VideoQueue()
