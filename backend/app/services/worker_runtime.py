import os
import queue
import threading
import uuid
from collections.abc import Callable, MutableMapping
from dataclasses import dataclass, field
from typing import Any, Optional, TypeVar


TERMINAL_STATUSES = ("done", "failed", "cancelled")

JobT = TypeVar("JobT")


@dataclass
class WorkerPoolState:
    started: bool = False
    lock: threading.Lock = field(default_factory=threading.Lock)


def ensure_worker_pool(
    state: WorkerPoolState,
    *,
    max_workers: int,
    target: Callable[[], None],
    name_prefix: str,
    logger,
    label: str,
) -> None:
    with state.lock:
        if state.started:
            return
        for worker_index in range(max_workers):
            thread = threading.Thread(
                target=target,
                name=f"{name_prefix}-{worker_index}",
                daemon=True,
            )
            thread.start()
        state.started = True
        logger.info(f"{label} worker pool started ({max_workers} workers)")


def get_registered_job(
    jobs: MutableMapping[str, JobT],
    lock: threading.Lock,
    job_id: str,
) -> Optional[JobT]:
    with lock:
        return jobs.get(job_id)


def cancel_registered_job(
    jobs: MutableMapping[str, Any],
    lock: threading.Lock,
    job_id: str,
) -> bool:
    with lock:
        job = jobs.get(job_id)
        if not job or job.status in TERMINAL_STATUSES:
            return False
        job.status = "cancelled"
        if job._proc:
            try:
                job._proc.kill()
            except Exception:
                pass
    return True


def enqueue_registered_job(
    jobs: MutableMapping[str, Any],
    lock: threading.Lock,
    task_queue: queue.Queue,
    job: Any,
    queue_item: Any | None = None,
) -> str:
    with lock:
        jobs[job.job_id] = job
    task_queue.put(job.job_id if queue_item is None else queue_item)
    return job.job_id


def new_job_id() -> str:
    return uuid.uuid4().hex


def cleanup_paths(paths: list[str]) -> None:
    for path in paths:
        try:
            if os.path.exists(path):
                os.remove(path)
        except OSError:
            pass


def process_queued_job(
    *,
    job_id: str,
    jobs: MutableMapping[str, Any],
    lock: threading.Lock,
    logger,
    cleanup_cancelled: Callable[[Any | None], None],
    picked_message: Callable[[Any], str],
    process: Callable[[Any], None],
) -> None:
    from app.services.global_semaphore import global_job_semaphore

    job = jobs.get(job_id)
    if not job or job.status == "cancelled":
        cleanup_cancelled(job)
        logger.info(f"[{job_id}] Skipped (cancelled before pickup)")
        return

    global_job_semaphore.acquire()
    try:
        with lock:
            job.status = "processing"
        logger.info(picked_message(job))
        process(job)
    finally:
        global_job_semaphore.release()
