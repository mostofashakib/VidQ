import logging
import os
import shutil
import uuid

from fastapi import HTTPException, UploadFile

from app.config import get_settings
from app.logging_utils import log_suppressed

logger = logging.getLogger(__name__)


def save_upload_file(file: UploadFile, *, prefix: str = "") -> tuple[str, str]:
    settings = get_settings()
    os.makedirs(settings.temp_storage_dir, exist_ok=True)

    original_name = file.filename or "video.mp4"
    ext = os.path.splitext(original_name)[1] or ".mp4"
    filename = f"{prefix}{uuid.uuid4().hex}{ext}"
    file_path = os.path.join(settings.temp_storage_dir, filename)

    try:
        with open(file_path, "wb") as saved_file:
            shutil.copyfileobj(file.file, saved_file)
    except Exception as error:
        try:
            os.remove(file_path)
        except OSError as cleanup_err:
            log_suppressed(logger, f"Could not remove partial upload {file_path}", cleanup_err, level="debug")
        raise HTTPException(status_code=500, detail=f"File upload failed: {error}")

    return file_path, original_name


def save_upload_files(files: list[UploadFile], *, prefix: str = "") -> tuple[list[str], list[str]]:
    saved_paths: list[str] = []
    original_names: list[str] = []
    try:
        for file in files:
            file_path, original_name = save_upload_file(file, prefix=prefix)
            saved_paths.append(file_path)
            original_names.append(original_name)
    except Exception:
        for path in saved_paths:
            try:
                os.remove(path)
            except OSError as cleanup_err:
                log_suppressed(logger, f"Could not remove partial upload {path}", cleanup_err, level="debug")
        raise

    return saved_paths, original_names
