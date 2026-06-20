# backend/tests/test_enhance.py
"""Tests for enhance video endpoints and worker."""
import io
import os
import shutil
import time
import pytest
from unittest.mock import patch

AUTH = {"Authorization": "Bearer test-token"}


def _fake_mp4() -> bytes:
    return b"\x00" * 2048


def _make_smart_popen():
    """FakePopen that creates expected output files based on command content."""

    def fake_popen_fn(cmd, stdout=None, stderr=None, **kwargs):
        cmd_list = list(cmd)

        class FP:
            def __init__(self):
                self.stdout = iter([])
                self.stderr = iter([])
                self.returncode = 0

            def wait(self):
                return 0

            def kill(self):
                pass

        # realesrgan: copy frames from -i dir to -o dir
        if "realesrgan-ncnn-vulkan" in cmd_list:
            i_idx = cmd_list.index("-i")
            o_idx = cmd_list.index("-o")
            in_dir = cmd_list[i_idx + 1]
            out_dir = cmd_list[o_idx + 1]
            os.makedirs(out_dir, exist_ok=True)
            if os.path.isdir(in_dir):
                for fn in os.listdir(in_dir):
                    shutil.copy(os.path.join(in_dir, fn), os.path.join(out_dir, fn))

        # segment split: create one chunk file
        elif "-segment_time" in cmd_list:
            pat = cmd_list[-1]  # e.g. /path/chunks/chunk_%04d.mp4
            chunk_dir = os.path.dirname(pat)
            os.makedirs(chunk_dir, exist_ok=True)
            with open(os.path.join(chunk_dir, "chunk_0000.mp4"), "wb") as f:
                f.write(b"fake-chunk" * 200)

        # frame extraction: write one JPEG into frames dir
        elif "-qscale:v" in cmd_list:
            pat = cmd_list[-1]  # e.g. /path/frames_0000/%08d.jpg
            frames_dir = os.path.dirname(pat)
            os.makedirs(frames_dir, exist_ok=True)
            with open(os.path.join(frames_dir, "00000001.jpg"), "wb") as f:
                f.write(b"fake-jpg" * 200)

        else:
            # Generic ffmpeg: write fake data to last arg if it looks like a file
            out = cmd_list[-1]
            if not out.startswith("-") and "%" not in out and not out.endswith(".txt"):
                parent = os.path.dirname(out)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(out, "wb") as f:
                    f.write(b"fake-data" * 200)

        return FP()

    return fake_popen_fn


# ── Route-level tests ──────────────────────────────────────────────────────

def test_enhance_creates_job(client):
    r = client.post(
        "/enhance-video",
        files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
        headers=AUTH,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "queued"
    assert "job_id" in data
    assert data["phase"] == "queued"
    assert data["progress"] == 0


def test_enhance_job_not_found(client):
    r = client.get("/enhance-jobs/nonexistent-id", headers=AUTH)
    assert r.status_code == 404


def test_cancel_enhance_job_not_found(client):
    r = client.delete("/enhance-jobs/nonexistent-id", headers=AUTH)
    assert r.status_code == 404


def test_enhance_status_returns_phase(client):
    r = client.post(
        "/enhance-video",
        files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
        headers=AUTH,
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]
    r2 = client.get(f"/enhance-jobs/{job_id}", headers=AUTH)
    assert r2.status_code == 200
    assert "phase" in r2.json()


# ── Worker tests ───────────────────────────────────────────────────────────

def test_enhance_missing_binary_marks_failed(client):
    """If realesrgan-ncnn-vulkan is not on PATH, job fails with a clear error."""
    with patch("app.services.enhance_worker.shutil.which", return_value=None):
        with patch("app.services.enhance_worker._probe_video", return_value=(30.0, 5.0, 640, 480)):
            r = client.post(
                "/enhance-video",
                files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
                headers=AUTH,
            )
            assert r.status_code == 200
            job_id = r.json()["job_id"]

            deadline = time.time() + 10
            result = {}
            while time.time() < deadline:
                r2 = client.get(f"/enhance-jobs/{job_id}", headers=AUTH)
                result = r2.json()
                if result["status"] in ("done", "failed"):
                    break
                time.sleep(0.1)

    assert result["status"] == "failed"
    assert "realesrgan-ncnn-vulkan" in (result.get("error") or "")


def test_enhance_job_completes(client):
    """Full pipeline with mocked subprocess produces done status and result_url."""
    with patch("app.services.enhance_worker.shutil.which",
               return_value="/usr/local/bin/realesrgan-ncnn-vulkan"):
        with patch("app.services.enhance_worker._probe_video",
                   return_value=(30.0, 5.0, 640, 480)):
            with patch("app.services.enhance_worker.subprocess.Popen",
                       side_effect=_make_smart_popen()):
                r = client.post(
                    "/enhance-video",
                    files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
                    headers=AUTH,
                )
                assert r.status_code == 200
                job_id = r.json()["job_id"]

                deadline = time.time() + 15
                result = {}
                while time.time() < deadline:
                    r2 = client.get(f"/enhance-jobs/{job_id}", headers=AUTH)
                    result = r2.json()
                    if result["status"] in ("done", "failed"):
                        break
                    time.sleep(0.1)

    assert result["status"] == "done", f"Expected done, got: {result}"
    assert result["result_url"] is not None
    assert result["progress"] == 100


def test_enhance_subprocess_failure_marks_failed(client):
    """If any subprocess exits non-zero, job is marked failed."""

    class FailingPopen:
        def __init__(self, cmd, stdout=None, stderr=None, **kwargs):
            self.stdout = iter([])
            self.stderr = iter(["error\n"])
            self.returncode = 1

        def wait(self):
            return 1

        def kill(self):
            pass

    with patch("app.services.enhance_worker.shutil.which",
               return_value="/usr/local/bin/realesrgan-ncnn-vulkan"):
        with patch("app.services.enhance_worker._probe_video",
                   return_value=(30.0, 5.0, 640, 480)):
            with patch("app.services.enhance_worker.subprocess.Popen", FailingPopen):
                r = client.post(
                    "/enhance-video",
                    files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
                    headers=AUTH,
                )
                assert r.status_code == 200
                job_id = r.json()["job_id"]

                deadline = time.time() + 10
                result = {}
                while time.time() < deadline:
                    r2 = client.get(f"/enhance-jobs/{job_id}", headers=AUTH)
                    result = r2.json()
                    if result["status"] in ("done", "failed"):
                        break
                    time.sleep(0.1)

    assert result["status"] == "failed"


def test_enhance_cancel_job(client):
    """Cancelling a job marks it cancelled."""
    r = client.post(
        "/enhance-video",
        files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
        headers=AUTH,
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    r2 = client.delete(f"/enhance-jobs/{job_id}", headers=AUTH)
    # 204 = cancelled; 404 = worker finished before cancel (race)
    assert r2.status_code in (204, 404)

    if r2.status_code == 204:
        r3 = client.get(f"/enhance-jobs/{job_id}", headers=AUTH)
        assert r3.json()["status"] == "cancelled"
