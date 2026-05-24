import os
import shutil
import uuid
import logging
from typing import List, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db, Video
from app.models import VideoOut
from app.routers.auth import verify_token
from app.services.upload_worker import cancel_job, get_job, start_upload_job

logger = logging.getLogger("UploadRouter")

router = APIRouter()


class UploadJobOut(BaseModel):
    job_id: str
    filename: str
    status: str
    video_id: Optional[int] = None
    error: Optional[str] = None


@router.post("/upload-video", response_model=UploadJobOut)
async def upload_video(
    file: UploadFile = File(...),
    token: str = Depends(verify_token),
):
    settings = get_settings()
    os.makedirs(settings.temp_storage_dir, exist_ok=True)

    original_name = file.filename or "video.mp4"
    ext = os.path.splitext(original_name)[1] or ".mp4"
    filename = f"{uuid.uuid4().hex}{ext}"
    file_path = os.path.join(settings.temp_storage_dir, filename)

    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    job_id = start_upload_job(file_path, original_name)
    logger.info(f"Upload started: job_id={job_id} file={original_name}")
    return UploadJobOut(job_id=job_id, filename=original_name, status="processing")


@router.get("/upload-jobs/{job_id}", response_model=UploadJobOut)
def get_upload_job(job_id: str, token: str = Depends(verify_token)):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return UploadJobOut(
        job_id=job.job_id,
        filename=job.filename,
        status=job.status,
        video_id=job.video_id,
        error=job.error,
    )


@router.delete("/upload-jobs/{job_id}", status_code=204)
def cancel_upload_job(job_id: str, token: str = Depends(verify_token)):
    ok = cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or not cancellable")
    return None


@router.get("/upload-videos", response_model=List[VideoOut])
def list_upload_videos(
    db: Session = Depends(get_db),
    token: str = Depends(verify_token),
):
    return (
        db.query(Video)
        .filter(Video.source == "upload")
        .order_by(Video.created_at.desc())
        .all()
    )
