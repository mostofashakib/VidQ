import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import Video, get_db
from app.routers.auth import verify_token
from app.routers.upload_utils import resolve_video_file_path, save_upload_file
from app.services.enhance_worker import cancel_job, get_job, start_enhance_job

logger = logging.getLogger("EnhanceRouter")

router = APIRouter()


class EnhanceJobOut(BaseModel):
    job_id: str
    status: str
    phase: str = "queued"
    progress: int = 0
    result_url: Optional[str] = None
    error: Optional[str] = None


@router.post("/enhance-video", response_model=EnhanceJobOut)
async def start_enhance(
    file: UploadFile = File(...),
    token: str = Depends(verify_token),
):
    file_path, _ = save_upload_file(file, prefix="enhance_in_")
    job_id = start_enhance_job(file_path)
    logger.info(f"Enhance queued: job_id={job_id}")
    return EnhanceJobOut(job_id=job_id, status="queued")


@router.post("/videos/{video_id}/enhance", response_model=EnhanceJobOut)
def enhance_video_by_id(
    video_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(verify_token),
):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    file_path, _ = resolve_video_file_path(video)
    job_id = start_enhance_job(file_path, title=video.title, cleanup_input=False)
    logger.info(f"Enhance pipeline queued for video {video_id}: job_id={job_id}")
    return EnhanceJobOut(job_id=job_id, status="queued")


@router.get("/enhance-jobs/{job_id}", response_model=EnhanceJobOut)
def get_enhance_status(job_id: str, token: str = Depends(verify_token)):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return EnhanceJobOut(
        job_id=job.job_id,
        status=job.status,
        phase=job.phase,
        progress=job.progress,
        result_url=job.result_url,
        error=job.error,
    )


@router.delete("/enhance-jobs/{job_id}", status_code=204)
def cancel_enhance_job(job_id: str, token: str = Depends(verify_token)):
    ok = cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or not cancellable")
    return None
