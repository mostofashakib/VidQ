"""Integration tests for the video upload endpoints."""
import io
import os
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


def test_webm_upload_processing_converts_to_mp4(tmp_path, monkeypatch):
    from app.services import upload_worker

    input_path = tmp_path / "clip.webm"
    input_path.write_bytes(b"webm-data" * 200)
    captured_cmd = {}

    class FakePopen:
        def __init__(self, cmd, stdout=None, stderr=None, text=None, encoding=None, errors=None, bufsize=None):
            captured_cmd["cmd"] = cmd
            self.stdout = iter(["out_time=00:00:15.000000\n"])
            self.stderr = iter([])
            self.returncode = 0
            with open(cmd[-1], "wb") as output:
                output.write(b"mp4-data" * 200)

        def wait(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(upload_worker.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(upload_worker, "probe_video_dimensions", lambda _: (1280, 720))
    monkeypatch.setattr(upload_worker.subprocess, "Popen", FakePopen)

    job = upload_worker.UploadJob("job-webm", "clip.webm")
    final_path = upload_worker._scale_to_720p(job, str(input_path), total_duration_s=30.0)

    assert final_path is not None
    assert final_path.endswith("_mp4.mp4")
    assert os.path.exists(final_path)
    assert not input_path.exists()
    assert "-vf" not in captured_cmd["cmd"]
    assert captured_cmd["cmd"][captured_cmd["cmd"].index("-c:a") + 1] == "aac"


def test_webm_upload_processing_sets_conversion_error(tmp_path, monkeypatch):
    from app.services import upload_worker

    input_path = tmp_path / "broken.webm"
    input_path.write_bytes(b"bad-webm-data" * 200)

    class FakePopen:
        def __init__(self, cmd, stdout=None, stderr=None, text=None, encoding=None, errors=None, bufsize=None):
            self.stdout = iter([])
            self.stderr = iter(["conversion failed\n"])
            self.returncode = 1

        def wait(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(upload_worker.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(upload_worker, "probe_video_dimensions", lambda _: (1280, 720))
    monkeypatch.setattr(upload_worker.subprocess, "Popen", FakePopen)

    job = upload_worker.UploadJob("job-webm-fail", "broken.webm")
    final_path = upload_worker._scale_to_720p(job, str(input_path), total_duration_s=30.0)

    assert final_path is None
    assert job.status == "failed"
    assert job.error == "Could not convert WebM to MP4"


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
