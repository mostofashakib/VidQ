"""End-to-end tests for trim video endpoints and worker."""
import io
import time
import pytest
from unittest.mock import patch

AUTH = {"Authorization": "Bearer test-token"}


def _fake_mp4() -> bytes:
    return b"\x00" * 2048


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

def test_trim_creates_job(client):
    r = client.post(
        "/trim-video",
        data={"start_time": "0.0", "end_time": "10.0"},
        files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
        headers=AUTH,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "queued"
    assert "job_id" in data


def test_trim_start_ge_end_returns_400(client):
    r = client.post(
        "/trim-video",
        data={"start_time": "10.0", "end_time": "5.0"},
        files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
        headers=AUTH,
    )
    assert r.status_code == 400


def test_trim_negative_start_time_returns_400(client):
    r = client.post(
        "/trim-video",
        data={"start_time": "-1.0", "end_time": "5.0"},
        files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
        headers=AUTH,
    )
    assert r.status_code == 400


def test_trim_job_not_found(client):
    r = client.get("/trim-jobs/nonexistent-job-id", headers=AUTH)
    assert r.status_code == 404


def test_cancel_trim_job_not_found(client):
    r = client.delete("/trim-jobs/nonexistent-job-id", headers=AUTH)
    assert r.status_code == 404


def test_trim_start_equal_end_returns_400(client):
    r = client.post(
        "/trim-video",
        data={"start_time": "5.0", "end_time": "5.0"},
        files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
        headers=AUTH,
    )
    assert r.status_code == 400


# ── Full pipeline tests (worker + mocked ffmpeg) ───────────────────────────

def test_trim_job_completes(client):
    """Job moves from queued → processing → done with progress = 100 and a result_url."""
    FakePopen = _make_fake_popen()
    with patch("app.services.trim_worker.subprocess.Popen", FakePopen):
        r = client.post(
            "/trim-video",
            data={"start_time": "0.0", "end_time": "10.0"},
            files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
            headers=AUTH,
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        deadline = time.time() + 10
        result = {}
        while time.time() < deadline:
            r2 = client.get(f"/trim-jobs/{job_id}", headers=AUTH)
            result = r2.json()
            if result["status"] in ("done", "failed"):
                break
            time.sleep(0.1)

    assert result["status"] == "done", f"Expected done, got: {result}"
    assert result["result_url"] is not None
    assert result["progress"] == 100


def test_trim_job_ffmpeg_failure_marks_failed(client):
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

    with patch("app.services.trim_worker.subprocess.Popen", FailingPopen):
        r = client.post(
            "/trim-video",
            data={"start_time": "0.0", "end_time": "10.0"},
            files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
            headers=AUTH,
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        deadline = time.time() + 10
        result = {}
        while time.time() < deadline:
            r2 = client.get(f"/trim-jobs/{job_id}", headers=AUTH)
            result = r2.json()
            if result["status"] in ("done", "failed"):
                break
            time.sleep(0.1)

    assert result["status"] == "failed"


def test_trim_cancel_job(client):
    """Cancelling a queued/processing job marks it cancelled."""
    r = client.post(
        "/trim-video",
        data={"start_time": "0.0", "end_time": "10.0"},
        files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
        headers=AUTH,
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    r2 = client.delete(f"/trim-jobs/{job_id}", headers=AUTH)
    # 204 = cancelled; 404 = worker finished before we could cancel (race)
    assert r2.status_code in (204, 404)

    if r2.status_code == 204:
        r3 = client.get(f"/trim-jobs/{job_id}", headers=AUTH)
        assert r3.json()["status"] == "cancelled"


def test_trim_job_progress_reaches_100(client):
    """progress reaches 100 on a successful trim."""
    progress_lines = [
        "out_time=00:00:03.000000\n",
        "out_time=00:00:07.000000\n",
        "out_time=00:00:10.000000\n",
        "progress=end\n",
    ]
    FakePopen = _make_fake_popen(progress_lines)
    with patch("app.services.trim_worker.subprocess.Popen", FakePopen):
        r = client.post(
            "/trim-video",
            data={"start_time": "0.0", "end_time": "10.0"},
            files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
            headers=AUTH,
        )
        job_id = r.json()["job_id"]

        deadline = time.time() + 10
        final = {}
        while time.time() < deadline:
            r2 = client.get(f"/trim-jobs/{job_id}", headers=AUTH)
            final = r2.json()
            if final["status"] == "done":
                break
            time.sleep(0.05)

    assert final["status"] == "done"
    assert final["progress"] == 100
