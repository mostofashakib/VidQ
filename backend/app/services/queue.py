"""
Async video recording queue.

Jobs are enqueued and processed in a background thread. The API returns
immediately with a job_id so the client can poll /queue/{job_id} for status.
"""
import threading
import uuid
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

logger = logging.getLogger("VideoQueue")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('[%(levelname)s] [Queue] %(message)s'))
    logger.addHandler(handler)


class JobStatus(str, Enum):
    QUEUED = "queued"
    PROCESSING = "processing"
    DONE = "done"
    FAILED = "failed"


@dataclass
class RecordingJob:
    job_id: str
    url: str
    category: str
    token: str
    status: JobStatus = JobStatus.QUEUED
    result: Optional[dict] = None
    error: Optional[str] = None


class VideoQueue:
    """Thread-safe singleton queue backed by a daemon worker thread."""

    def __init__(self):
        self._jobs: dict[str, RecordingJob] = {}
        self._queue: list[str] = []          # ordered list of job_ids
        self._lock = threading.Lock()
        self._event = threading.Event()
        self._worker = threading.Thread(target=self._run, daemon=True)
        self._worker.start()
        logger.info("VideoQueue worker thread started.")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def enqueue(self, url: str, category: str, token: str) -> RecordingJob:
        job = RecordingJob(
            job_id=uuid.uuid4().hex,
            url=url,
            category=category,
            token=token,
        )
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
        logger.info(f"Processing job {job.job_id} — {job.url}")
        job.status = JobStatus.PROCESSING
        
        async def _async_job():
            # Import here to avoid circular deps at module load
            from app.services.scraper import run_extraction, USER_AGENTS
            from app.state import llm_manager
            from app.routers.video import call_llm_with_html_and_screenshot
            import random

            user_agent = random.choice(USER_AGENTS)
            
            # Step 1: Extract/Record
            html, screenshot_b64, network_video_urls, thumbnail_url, temp_video_url = await run_extraction(
                url=job.url,
                user_agent=user_agent,
                llm_manager=llm_manager,
                max_record_seconds=10800,   # up to 3 hours as per latest scraper default
            )

            # Step 2: LLM Metadata extraction
            result = await call_llm_with_html_and_screenshot(
                llm_manager, html, screenshot_b64, network_video_urls, thumbnail_url
            )
            result["thumbnail"] = result.get("thumbnail") or thumbnail_url
            if not result.get("video_url") and temp_video_url:
                result["video_url"] = temp_video_url
            
            return result, temp_video_url

        try:
            import asyncio
            result, temp_video_url = asyncio.run(_async_job())

            # Step 3: Persist to DB (Keep DB block sync as it uses standard SQLAlchemy session)
            from app.db import SessionLocal, Video
            db = SessionLocal()
            try:
                video_url = result.get("video_url") or job.url
                title = result.get("title")
                duration = result.get("duration")
                thumbnail = result.get("thumbnail")
                # Deduplicate
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
                    logger.info(f"Job {job.job_id} — video already exists, skipping DB insert.")
            finally:
                db.close()

            job.result = result
            job.status = JobStatus.DONE
            logger.info(f"Job {job.job_id} completed successfully.")

        except Exception as e:
            logger.error(f"Job {job.job_id} failed: {e}", exc_info=True)
            job.status = JobStatus.FAILED
            job.error = str(e)


# Module-level singleton — shared across all requests
video_queue = VideoQueue()
