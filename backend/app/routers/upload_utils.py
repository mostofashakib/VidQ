import logging
import os
import shutil
import uuid
from urllib.parse import urlparse

import requests
from fastapi import HTTPException, UploadFile

from app.config import get_settings
from app.logging_utils import log_suppressed
from app.services.url_safety import filename_from_url, is_safe_url

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


def resolve_video_file_path(video) -> tuple[str, str]:
    """
    Returns (local_file_path, display_filename) for a Video database record.
    If the video is already in temp_storage, returns its existing local path.
    If it is an external URL, downloads it to temp_storage for processing.
    Raises HTTPException(404) or HTTPException(500) if inaccessible.
    """
    settings = get_settings()
    video_url = (video.url or "").strip()
    _parsed = urlparse(video_url)
    display_name = video.title or f"video_{video.id}"

    # 1. Local temp_storage file (relative or absolute URL)
    if "/temp_storage/" in _parsed.path or video_url.startswith("/temp_storage/"):
        filename = filename_from_url(video_url)
        filepath = os.path.join(settings.temp_storage_dir, filename)
        if os.path.exists(filepath) and os.path.getsize(filepath) > 0:
            return filepath, f"{display_name}.mp4"

    # 2. Check by filename if stored directly
    filename = filename_from_url(video_url)
    direct_path = os.path.join(settings.temp_storage_dir, filename)
    if os.path.exists(direct_path) and os.path.getsize(direct_path) > 0:
        return direct_path, f"{display_name}.mp4"

    # 3. External safe URL: stream into temp_storage
    if is_safe_url(video_url):
        ext_filename = filename_from_url(video_url)
        ext = os.path.splitext(ext_filename)[1] or ".mp4"
        out_filename = f"cached_{uuid.uuid4().hex}{ext}"
        out_path = os.path.join(settings.temp_storage_dir, out_filename)
        try:
            with requests.get(
                video_url, stream=True, timeout=30,
                headers={"User-Agent": "Mozilla/5.0"},
            ) as r:
                r.raise_for_status()
                with open(out_path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            f.write(chunk)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                return out_path, f"{display_name}{ext}"
        except Exception as exc:
            if os.path.exists(out_path):
                try:
                    os.remove(out_path)
                except OSError:
                    pass
            raise HTTPException(status_code=500, detail=f"Failed to fetch video source: {exc}")

    raise HTTPException(status_code=404, detail="Video file not found or inaccessible")
