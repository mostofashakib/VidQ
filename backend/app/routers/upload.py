import os
import shutil
import uuid
import logging

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session
from typing import List

from app.config import get_settings
from app.db import get_db, Video
from app.models import VideoOut
from app.routers.auth import verify_token
from app.services.video_utils import ensure_min_quality
from app.services.scraper.media import _probe_file_duration

logger = logging.getLogger("UploadRouter")

router = APIRouter()


@router.post("/upload-video", response_model=VideoOut)
async def upload_video(
    file: UploadFile = File(...),
    category: str = Form(default="uploads"),
    db: Session = Depends(get_db),
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

    final_path = ensure_min_quality(file_path)
    final_filename = os.path.basename(final_path)
    video_url = f"{settings.base_url}/temp_storage/{final_filename}"

    duration: float | None = None
    try:
        duration = _probe_file_duration(final_path)
    except Exception:
        pass

    title = os.path.splitext(original_name)[0]

    db_video = Video(
        url=video_url.strip().lower(),
        category=category,
        title=title,
        duration=duration,
        source="upload",
    )
    db.add(db_video)
    db.commit()
    db.refresh(db_video)
    logger.info(f"Uploaded video saved: id={db_video.id} path={final_path}")
    return db_video


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
