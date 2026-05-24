import os
import re
import subprocess
import threading
import logging
from typing import Optional

import imageio_ffmpeg

from app.config import get_settings
from app.db import SessionLocal, Video
from app.services.video_utils import probe_video_dimensions

logger = logging.getLogger("UploadWorker")

_jobs: dict[str, "UploadJob"] = {}
_lock = threading.Lock()


class UploadJob:
    def __init__(self, job_id: str, filename: str):
        self.job_id = job_id
        self.filename = filename
        self.status = "processing"  # processing | done | failed | cancelled
        self.video_id: Optional[int] = None
        self.error: Optional[str] = None
        self._proc: Optional[subprocess.Popen] = None


def get_job(job_id: str) -> Optional[UploadJob]:
    with _lock:
        return _jobs.get(job_id)


def cancel_job(job_id: str) -> bool:
    with _lock:
        job = _jobs.get(job_id)
        if not job or job.status != "processing":
            return False
        job.status = "cancelled"
        if job._proc:
            try:
                job._proc.kill()
            except Exception:
                pass
    return True


def start_upload_job(file_path: str, original_name: str) -> str:
    import uuid
    job_id = uuid.uuid4().hex
    job = UploadJob(job_id=job_id, filename=original_name)
    with _lock:
        _jobs[job_id] = job
    thread = threading.Thread(target=_worker, args=(job_id, file_path, original_name), daemon=True)
    thread.start()
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


def _scale_to_720p(job: UploadJob, file_path: str) -> Optional[str]:
    """Scale to 720p via cancellable Popen. Returns final path, or None on cancel/failure."""
    dims = probe_video_dimensions(file_path)
    if dims is None or dims[1] == 720:
        return file_path

    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    base, ext = os.path.splitext(file_path)
    out_path = f"{base}_720p{ext}"
    cmd = [
        ffmpeg_exe, "-y", "-i", file_path,
        "-vf", "scale=-2:720:flags=lanczos",
        "-c:v", "libx264", "-crf", "18", "-preset", "slow", "-c:a", "copy",
        out_path,
    ]

    logger.info(f"[{job.job_id}] Scaling {os.path.basename(file_path)} to 720p")
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    with _lock:
        job._proc = proc

    proc.wait()

    with _lock:
        job._proc = None
        cancelled = job.status == "cancelled"

    if cancelled:
        for p in (file_path, out_path):
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass
        return None

    if proc.returncode != 0 or not os.path.exists(out_path) or os.path.getsize(out_path) < 1000:
        stderr = b""
        try:
            stderr = proc.stderr.read()
        except Exception:
            pass
        logger.error(f"[{job.job_id}] ffmpeg failed: {stderr.decode(errors='replace')[-200:]}")
        with _lock:
            job.status = "failed"
            job.error = "Video scaling failed"
        try:
            os.remove(file_path)
        except OSError:
            pass
        return None

    try:
        os.remove(file_path)
    except OSError:
        pass
    logger.info(f"[{job.job_id}] Scaled → {os.path.basename(out_path)}")
    return out_path


def _worker(job_id: str, file_path: str, original_name: str):
    job = _jobs[job_id]
    settings = get_settings()
    title = os.path.splitext(original_name)[0]

    try:
        final_path = _scale_to_720p(job, file_path)
        if final_path is None:
            return  # cancelled or failed — status already set

        duration = _probe_duration(final_path)
        final_filename = os.path.basename(final_path)
        video_url = f"{settings.base_url}/temp_storage/{final_filename}"

        db = SessionLocal()
        try:
            db_video = Video(
                url=video_url.strip().lower(),
                category="uploads",
                title=title,
                duration=duration,
                source="upload",
            )
            db.add(db_video)
            db.commit()
            db.refresh(db_video)
            with _lock:
                if job.status == "processing":
                    job.status = "done"
                    job.video_id = db_video.id
            logger.info(f"[{job_id}] Done: video_id={db_video.id}")
        finally:
            db.close()

    except Exception as e:
        logger.error(f"[{job_id}] Worker error: {e}", exc_info=True)
        with _lock:
            if job.status == "processing":
                job.status = "failed"
                job.error = str(e)
        try:
            if os.path.exists(file_path):
                os.remove(file_path)
        except OSError:
            pass
