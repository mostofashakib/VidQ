import logging
from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from app.routers.auth import verify_token
from app.routers.upload_utils import save_upload_file
from app.services.trim_worker import cancel_job, get_job, start_trim_job

logger = logging.getLogger("TrimRouter")

router = APIRouter()


class TrimJobOut(BaseModel):
    job_id: str
    status: str
    progress: int = 0
    result_url: Optional[str] = None
    error: Optional[str] = None


@router.post("/trim-video", response_model=TrimJobOut)
async def start_trim(
    file: UploadFile = File(...),
    start_time: float = Form(...),
    end_time: float = Form(...),
    token: str = Depends(verify_token),
):
    if start_time < 0 or end_time <= start_time:
        raise HTTPException(status_code=400, detail="start_time must be >= 0 and < end_time")

    file_path, _ = save_upload_file(file, prefix="trim_in_")
    job_id = start_trim_job(file_path, start_time, end_time)
    logger.info(f"Trim queued: job_id={job_id} start={start_time} end={end_time}")
    return TrimJobOut(job_id=job_id, status="queued")


@router.get("/trim-jobs/{job_id}", response_model=TrimJobOut)
def get_trim_status(job_id: str, token: str = Depends(verify_token)):
    job = get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return TrimJobOut(
        job_id=job.job_id,
        status=job.status,
        progress=job.progress,
        result_url=job.result_url,
        error=job.error,
    )


@router.delete("/trim-jobs/{job_id}", status_code=204)
def cancel_trim_job(job_id: str, token: str = Depends(verify_token)):
    ok = cancel_job(job_id)
    if not ok:
        raise HTTPException(status_code=404, detail="Job not found or not cancellable")
    return None
