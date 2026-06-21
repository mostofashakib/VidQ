"""End-to-end tests for combine video endpoints and worker."""
import io
import os
import subprocess
import time
import pytest
from unittest.mock import patch

import imageio_ffmpeg

from app.config import get_settings
from app.services.video_utils import probe_video_dimensions

AUTH = {"Authorization": "Bearer test-token"}


def _fake_mp4() -> bytes:
    return b"\x00" * 2048


def _make_test_video_bytes(
    tmp_path,
    filename: str,
    *,
    size: str,
    frequency: int,
) -> bytes:
    path = tmp_path / filename
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg_exe, "-y",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate=12:duration=1",
            "-f", "lavfi", "-i", f"sine=frequency={frequency}:duration=1",
            "-c:v", "libx264",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            str(path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return path.read_bytes()


def _make_fake_popen(progress_lines=None):
    """FakePopen that writes a real output file so the worker considers ffmpeg successful."""
    if progress_lines is None:
        progress_lines = ["out_time=00:00:05.000000\n", "progress=end\n"]

    class FakePopen:
        def __init__(self, cmd, stdout=None, stderr=None, text=None,
                     encoding=None, errors=None, bufsize=None):
            # Write fake output so size > 1000 check passes
            with open(cmd[-1], "wb") as f:
                f.write(b"fake-mp4-data" * 200)
            self.stdout = iter(progress_lines)
            self.stderr = iter([])
            self.returncode = 0

        def wait(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    return FakePopen


# ── Route-level tests (no worker needed) ───────────────────────────────────

def test_combine_creates_job(client):
    r = client.post(
        "/combine-video",
        files=[
            ("files", ("a.mp4", io.BytesIO(_fake_mp4()), "video/mp4")),
            ("files", ("b.mp4", io.BytesIO(_fake_mp4()), "video/mp4")),
        ],
        headers=AUTH,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "queued"
    assert "job_id" in data
    assert data["total_clips"] == 2


def test_combine_requires_at_least_two_files(client):
    r = client.post(
        "/combine-video",
        files=[("files", ("only.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
        headers=AUTH,
    )
    assert r.status_code == 400


def test_combine_no_files_returns_422(client):
    r = client.post("/combine-video", headers=AUTH)
    assert r.status_code == 422


def test_combine_job_not_found(client):
    r = client.get("/combine-jobs/nonexistent-job-id", headers=AUTH)
    assert r.status_code == 404


def test_cancel_combine_job_not_found(client):
    r = client.delete("/combine-jobs/nonexistent-job-id", headers=AUTH)
    assert r.status_code == 404


# ── Full pipeline tests (worker + mocked ffmpeg) ───────────────────────────

def test_combine_job_completes(client):
    """Job moves from queued → processing → done with overall_progress = 100."""
    FakePopen = _make_fake_popen()
    with (
        patch("app.services.combine_worker._probe_duration", return_value=10.0),
        patch("app.services.combine_worker.subprocess.Popen", FakePopen),
    ):
        r = client.post(
            "/combine-video",
            files=[
                ("files", ("a.mp4", io.BytesIO(_fake_mp4()), "video/mp4")),
                ("files", ("b.mp4", io.BytesIO(_fake_mp4()), "video/mp4")),
            ],
            headers=AUTH,
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        deadline = time.time() + 10
        result = {}
        while time.time() < deadline:
            r2 = client.get(f"/combine-jobs/{job_id}", headers=AUTH)
            result = r2.json()
            if result["status"] in ("done", "failed"):
                break
            time.sleep(0.1)

    assert result["status"] == "done", f"Expected done, got: {result}"
    assert result["result_url"] is not None
    assert result["overall_progress"] == 100


def test_combine_job_overall_progress_reaches_100(client):
    """overall_progress increments through normalizing (0→40%) then concatenating (40→100%)."""
    progress_lines = [
        "out_time=00:00:03.000000\n",
        "out_time=00:00:07.000000\n",
        "out_time=00:00:10.000000\n",
        "progress=end\n",
    ]
    FakePopen = _make_fake_popen(progress_lines)
    with (
        patch("app.services.combine_worker._probe_duration", return_value=10.0),
        patch("app.services.combine_worker.subprocess.Popen", FakePopen),
    ):
        r = client.post(
            "/combine-video",
            files=[
                ("files", ("a.mp4", io.BytesIO(_fake_mp4()), "video/mp4")),
                ("files", ("b.mp4", io.BytesIO(_fake_mp4()), "video/mp4")),
            ],
            headers=AUTH,
        )
        job_id = r.json()["job_id"]

        deadline = time.time() + 10
        final = {}
        while time.time() < deadline:
            r2 = client.get(f"/combine-jobs/{job_id}", headers=AUTH)
            final = r2.json()
            if final["status"] == "done":
                break
            time.sleep(0.05)

    assert final["status"] == "done"
    assert final["overall_progress"] == 100


def test_combine_job_three_clips(client):
    """Works with more than 2 clips."""
    FakePopen = _make_fake_popen()
    with (
        patch("app.services.combine_worker._probe_duration", return_value=5.0),
        patch("app.services.combine_worker.subprocess.Popen", FakePopen),
    ):
        r = client.post(
            "/combine-video",
            files=[
                ("files", ("a.mp4", io.BytesIO(_fake_mp4()), "video/mp4")),
                ("files", ("b.mp4", io.BytesIO(_fake_mp4()), "video/mp4")),
                ("files", ("c.mp4", io.BytesIO(_fake_mp4()), "video/mp4")),
            ],
            headers=AUTH,
        )
        assert r.status_code == 200
        assert r.json()["total_clips"] == 3
        job_id = r.json()["job_id"]

        deadline = time.time() + 10
        result = {}
        while time.time() < deadline:
            r2 = client.get(f"/combine-jobs/{job_id}", headers=AUTH)
            result = r2.json()
            if result["status"] in ("done", "failed"):
                break
            time.sleep(0.1)

    assert result["status"] == "done"


def test_combine_job_with_different_720p_widths_completes(client, tmp_path):
    """Real ffmpeg regression: xfade requires matching dimensions."""
    first_video = _make_test_video_bytes(
        tmp_path,
        "wide.mp4",
        size="1280x720",
        frequency=440,
    )
    second_video = _make_test_video_bytes(
        tmp_path,
        "narrow.mp4",
        size="960x720",
        frequency=880,
    )

    r = client.post(
        "/combine-video",
        files=[
            ("files", ("wide.mp4", io.BytesIO(first_video), "video/mp4")),
            ("files", ("narrow.mp4", io.BytesIO(second_video), "video/mp4")),
        ],
        headers=AUTH,
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    deadline = time.time() + 30
    result = {}
    while time.time() < deadline:
        r2 = client.get(f"/combine-jobs/{job_id}", headers=AUTH)
        result = r2.json()
        if result["status"] in ("done", "failed"):
            break
        time.sleep(0.1)

    assert result["status"] == "done", f"Expected done, got: {result}"
    assert result["result_url"] is not None
    output_filename = result["result_url"].rsplit("/", 1)[-1]
    output_path = os.path.join(get_settings().temp_storage_dir, output_filename)
    assert os.path.getsize(output_path) > 1000
    assert probe_video_dimensions(output_path) == (1280, 720)


def test_combine_downscales_1080p_to_high_quality_720p(client, tmp_path):
    first_video = _make_test_video_bytes(
        tmp_path,
        "full_hd.mp4",
        size="1920x1080",
        frequency=440,
    )
    second_video = _make_test_video_bytes(
        tmp_path,
        "hd.mp4",
        size="1280x720",
        frequency=880,
    )

    r = client.post(
        "/combine-video",
        files=[
            ("files", ("full_hd.mp4", io.BytesIO(first_video), "video/mp4")),
            ("files", ("hd.mp4", io.BytesIO(second_video), "video/mp4")),
        ],
        headers=AUTH,
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    deadline = time.time() + 30
    result = {}
    while time.time() < deadline:
        r2 = client.get(f"/combine-jobs/{job_id}", headers=AUTH)
        result = r2.json()
        if result["status"] in ("done", "failed"):
            break
        time.sleep(0.1)

    assert result["status"] == "done", f"Expected done, got: {result}"
    output_filename = result["result_url"].rsplit("/", 1)[-1]
    output_path = os.path.join(get_settings().temp_storage_dir, output_filename)
    assert probe_video_dimensions(output_path) == (1280, 720)


def test_combine_cancel_job(client):
    """Cancelling a queued/processing job marks it cancelled."""
    r = client.post(
        "/combine-video",
        files=[
            ("files", ("a.mp4", io.BytesIO(_fake_mp4()), "video/mp4")),
            ("files", ("b.mp4", io.BytesIO(_fake_mp4()), "video/mp4")),
        ],
        headers=AUTH,
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    r2 = client.delete(f"/combine-jobs/{job_id}", headers=AUTH)
    # 204 = cancelled; 404 = worker finished before we could cancel (race)
    assert r2.status_code in (204, 404)

    if r2.status_code == 204:
        r3 = client.get(f"/combine-jobs/{job_id}", headers=AUTH)
        assert r3.json()["status"] == "cancelled"


def test_combine_job_ffmpeg_failure_marks_failed(client):
    """If ffmpeg exits non-zero the job is marked failed, not done."""

    class FailingPopen:
        def __init__(self, cmd, stdout=None, stderr=None, text=None,
                     encoding=None, errors=None, bufsize=None):
            self.stdout = iter([])
            self.stderr = iter(["ffmpeg: error\n"])
            self.returncode = 1

        def wait(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    with (
        patch("app.services.combine_worker._probe_duration", return_value=10.0),
        patch("app.services.combine_worker.subprocess.Popen", FailingPopen),
    ):
        r = client.post(
            "/combine-video",
            files=[
                ("files", ("a.mp4", io.BytesIO(_fake_mp4()), "video/mp4")),
                ("files", ("b.mp4", io.BytesIO(_fake_mp4()), "video/mp4")),
            ],
            headers=AUTH,
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        deadline = time.time() + 10
        result = {}
        while time.time() < deadline:
            r2 = client.get(f"/combine-jobs/{job_id}", headers=AUTH)
            result = r2.json()
            if result["status"] in ("done", "failed"):
                break
            time.sleep(0.1)

    assert result["status"] == "failed"
