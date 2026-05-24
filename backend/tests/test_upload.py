"""Integration tests for the video upload endpoints."""
import io
import time
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
    return b"\x00" * 1024


def test_upload_video_creates_job(client):
    r = client.post(
        "/upload-video",
        files={"file": ("test.mp4", io.BytesIO(_fake_mp4_bytes()), "video/mp4")},
        headers=AUTH,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "queued"
    assert "job_id" in data
    assert data["filename"] == "test.mp4"


def test_upload_job_completes_and_video_appears(client):
    with patch("app.services.upload_worker.probe_video_dimensions", return_value=None), \
         patch("app.services.upload_worker._probe_duration", return_value=30.0):
        r = client.post(
            "/upload-video",
            files={"file": ("myvid.mp4", io.BytesIO(_fake_mp4_bytes()), "video/mp4")},
            headers=AUTH,
        )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    # Poll until done (max 5s)
    deadline = time.time() + 5
    status_data = {}
    while time.time() < deadline:
        r2 = client.get(f"/upload-jobs/{job_id}", headers=AUTH)
        status_data = r2.json()
        if status_data["status"] != "processing":
            break
        time.sleep(0.05)

    assert status_data["status"] == "done"
    assert status_data["video_id"] is not None

    r3 = client.get("/upload-videos", headers=AUTH)
    videos = r3.json()
    matched = [v for v in videos if v["id"] == status_data["video_id"]]
    assert matched, "Completed job's video not found in /upload-videos"
    assert matched[0]["title"] == "myvid"
    assert matched[0]["source"] == "upload"


def test_get_upload_job_not_found(client):
    r = client.get("/upload-jobs/nonexistent", headers=AUTH)
    assert r.status_code == 404


def test_cancel_upload_job_not_found(client):
    r = client.delete("/upload-jobs/nonexistent", headers=AUTH)
    assert r.status_code == 404


def test_upload_no_file_returns_422(client):
    r = client.post("/upload-video", headers=AUTH)
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
        category="uploads", title="Upload Video", source="upload",
        created_at=datetime.utcnow(),
    )
    db_session.add_all([url_video, upload_video])
    db_session.commit()

    r = client.get("/upload-videos", headers=AUTH)
    assert r.status_code == 200
    titles = [v["title"] for v in r.json()]
    assert "Upload Video" in titles
    assert "URL Video" not in titles
