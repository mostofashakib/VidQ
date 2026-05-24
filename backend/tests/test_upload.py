"""Integration tests for the video upload endpoints."""
import io
import os
import pytest
from unittest.mock import patch

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def clean_videos(db_session):
    from app.db import Video
    db_session.query(Video).delete()
    db_session.commit()
    yield


def _fake_mp4_bytes() -> bytes:
    """Minimal valid-looking file content for tests (not a real video)."""
    return b"\x00" * 1024


def test_upload_video_returns_200(client):
    with patch("app.routers.upload.ensure_min_quality", side_effect=lambda p: p), \
         patch("app.routers.upload._probe_file_duration", return_value=30.0):
        r = client.post(
            "/upload-video",
            files={"file": ("test_video.mp4", io.BytesIO(_fake_mp4_bytes()), "video/mp4")},
            data={"category": "test"},
            headers=AUTH,
        )
    assert r.status_code == 200
    data = r.json()
    assert data["source"] == "upload"
    assert data["category"] == "test"
    assert data["title"] == "test_video"
    assert data["duration"] == 30.0
    assert "temp_storage" in data["url"]


def test_upload_video_missing_category_returns_422(client):
    r = client.post(
        "/upload-video",
        files={"file": ("video.mp4", io.BytesIO(_fake_mp4_bytes()), "video/mp4")},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_list_upload_videos_empty(client):
    r = client.get("/upload-videos", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == []


def test_list_upload_videos_returns_only_uploads(client, db_session):
    from app.db import Video
    from datetime import datetime
    from app.config import get_settings

    settings = get_settings()
    url_video = Video(url="https://example.com/url.mp4", category="test",
                      title="URL Video", source="url", created_at=datetime.utcnow())
    upload_video = Video(
        url=f"{settings.base_url}/temp_storage/up.mp4",
        category="test", title="Upload Video", source="upload",
        created_at=datetime.utcnow(),
    )
    db_session.add_all([url_video, upload_video])
    db_session.commit()

    r = client.get("/upload-videos", headers=AUTH)
    assert r.status_code == 200
    titles = [v["title"] for v in r.json()]
    assert "Upload Video" in titles
    assert "URL Video" not in titles
