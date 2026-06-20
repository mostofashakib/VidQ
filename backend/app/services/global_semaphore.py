"""
Global concurrency gate shared across all job types (download, upload, combine, translate).
At most MAX_GLOBAL_JOBS may run simultaneously; excess jobs remain queued in their
per-type task queues until a slot opens.
"""
import threading

MAX_GLOBAL_JOBS = 5

global_job_semaphore = threading.BoundedSemaphore(MAX_GLOBAL_JOBS)
