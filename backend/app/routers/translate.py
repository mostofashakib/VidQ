import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import Video, get_db
from app.routers.auth import verify_token
from app.routers.upload_utils import resolve_video_file_path, save_upload_file
from app.services.translate_worker import cancel_job, get_job, start_translate_job

logger = logging.getLogger("TranslateRouter")

router = APIRouter()


class TranslateJobOut(BaseModel):
    job_id: str
    filename: str
    status: str
    phase: str = "queued"
    overall_progress: int = 0
    chunk_index: int = 0
    total_chunks: int = 0
    result_url: Optional[str] = None
    error: Optional[str] = None


@router.post("/translate-video", response_model=TranslateJobOut)
async def translate_video(
    file: UploadFile = File(...),
    token: str = Depends(verify_token),
):
    file_path, original_name = save_upload_file(file, prefix="translate_in_")
    job_id = start_translate_job(file_path, original_name)
    logger.info(f"Translate queued: job_id={job_id} file={original_name}")
    return TranslateJobOut(job_id=job_id, filename=original_name, status="queued")


@router.post("/videos/{video_id}/translate", response_model=TranslateJobOut)
def translate_video_by_id(
    video_id: int,
    db: Session = Depends(get_db),
    token: str = Depends(verify_token),
):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    file_path, original_name = resolve_video_file_path(video)
    job_id = start_translate_job(file_path, original_name, save_to_db=True, cleanup_input=False)
    logger.info(f"Translate pipeline queued for video {video_id}: job_id={job_id} file={original_name}")
    return TranslateJobOut(job_id=job_id, filename=original_name, status="queued")


@router.get("/translate-jobs/{job_id}", response_model=TranslateJobOut)
def get_translate_job(job_id: str, token: str = Depends(verify_token)):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return TranslateJobOut(
        job_id=job.job_id,
        filename=job.filename,
        status=job.status,
        phase=job.phase,
        overall_progress=job.overall_progress,
        chunk_index=job.chunk_index,
        total_chunks=job.total_chunks,
        result_url=job.result_url,
        error=job.error,
    )


@router.delete("/translate-jobs/{job_id}", status_code=204)
def cancel_translate_job(job_id: str, token: str = Depends(verify_token)):
    ok = cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or not cancellable")
    return None
