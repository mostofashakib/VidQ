import os
import shutil
import uuid
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel

from app.config import get_settings
from app.routers.auth import verify_token
from app.services.combine_worker import cancel_job, get_job, start_combine_job

logger = logging.getLogger("CombineRouter")

router = APIRouter()


class CombineJobOut(BaseModel):
    job_id: str
    status: str
    phase: str = "queued"
    overall_progress: int = 0
    clip_index: int = 0
    total_clips: int = 0
    result_url: Optional[str] = None
    error: Optional[str] = None


@router.post("/combine-video", response_model=CombineJobOut)
async def combine_video(
    files: List[UploadFile] = File(...),
    token: str = Depends(verify_token),
):
    if len(files) < 2:
        raise HTTPException(status_code=400, detail="At least 2 video files are required")
    if len(files) > 20:
        raise HTTPException(status_code=400, detail="Maximum 20 files allowed")

    settings = get_settings()
    os.makedirs(settings.temp_storage_dir, exist_ok=True)

    saved_paths: list[str] = []
    original_names: list[str] = []

    try:
        for file in files:
            original_name = file.filename or "video.mp4"
            ext = os.path.splitext(original_name)[1] or ".mp4"
            filename = f"combine_in_{uuid.uuid4().hex}{ext}"
            file_path = os.path.join(settings.temp_storage_dir, filename)
            with open(file_path, "wb") as f:
                shutil.copyfileobj(file.file, f)
            saved_paths.append(file_path)
            original_names.append(original_name)
    except Exception as e:
        for p in saved_paths:
            try:
                os.remove(p)
            except OSError:
                pass
        raise HTTPException(status_code=500, detail=f"File upload failed: {e}")

    job_id = start_combine_job(saved_paths, original_names)
    logger.info(f"Combine queued: job_id={job_id} clips={len(files)}")
    return CombineJobOut(
        job_id=job_id,
        status="queued",
        total_clips=len(files),
    )


@router.get("/combine-jobs/{job_id}", response_model=CombineJobOut)
def get_combine_job(job_id: str, token: str = Depends(verify_token)):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return CombineJobOut(
        job_id=job.job_id,
        status=job.status,
        phase=job.phase,
        overall_progress=job.overall_progress,
        clip_index=job.clip_index,
        total_clips=job.total_clips,
        result_url=job.result_url,
        error=job.error,
    )


@router.delete("/combine-jobs/{job_id}", status_code=204)
def cancel_combine_job(job_id: str, token: str = Depends(verify_token)):
    ok = cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or not cancellable")
    return None
