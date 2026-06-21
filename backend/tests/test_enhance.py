# backend/tests/test_enhance.py
"""Tests for enhance video endpoints and worker."""
import io
import os
import shutil
import subprocess
import time
import pytest
from unittest.mock import patch

import imageio_ffmpeg

from app.config import get_settings

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
        if any(str(part).endswith("realesrgan-ncnn-vulkan") for part in cmd_list):
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

        # frame extraction: write one PNG into frames dir
        elif str(cmd_list[-1]).endswith("%08d.png"):
            pat = cmd_list[-1]  # e.g. /path/frames_0000/%08d.png
            frames_dir = os.path.dirname(pat)
            os.makedirs(frames_dir, exist_ok=True)
            with open(os.path.join(frames_dir, "00000001.png"), "wb") as f:
                f.write(b"fake-png" * 200)

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


def _wait_for_enhance_job(client, job_id: str, timeout: int = 15) -> dict:
    deadline = time.time() + timeout
    result = {}
    while time.time() < deadline:
        response = client.get(f"/enhance-jobs/{job_id}", headers=AUTH)
        result = response.json()
        if result["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    return result


def _make_dummy_video_bytes(tmp_path) -> bytes:
    video_path = tmp_path / "dummy.mp4"
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    result = subprocess.run(
        [
            ffmpeg_exe, "-y",
            "-f", "lavfi",
            "-i", "testsrc=size=64x64:rate=2",
            "-t", "1",
            "-pix_fmt", "yuv420p",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    return video_path.read_bytes()


def _write_fake_realesrgan(tmp_path) -> str:
    binary_path = tmp_path / "realesrgan-ncnn-vulkan"
    binary_path.write_text(
        """#!/usr/bin/env python3
import os
import shutil
import sys

args = sys.argv[1:]
input_dir = args[args.index("-i") + 1]
output_dir = args[args.index("-o") + 1]
os.makedirs(output_dir, exist_ok=True)
for name in sorted(os.listdir(input_dir)):
    source = os.path.join(input_dir, name)
    if os.path.isfile(source):
        shutil.copy2(source, os.path.join(output_dir, name))
""",
        encoding="utf-8",
    )
    binary_path.chmod(0o755)
    return str(binary_path)


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


def test_enhance_route_processes_dummy_video_end_to_end(client, tmp_path):
    """Route saves a real video, runs ffmpeg, calls Real-ESRGAN, and returns output."""
    dummy_video = _make_dummy_video_bytes(tmp_path)
    fake_realesrgan = _write_fake_realesrgan(tmp_path)

    with patch("app.services.enhance_worker._resolve_real_esrgan_bin",
               return_value=fake_realesrgan):
        response = client.post(
            "/enhance-video",
            files=[("file", ("dummy.mp4", io.BytesIO(dummy_video), "video/mp4"))],
            headers=AUTH,
        )
        assert response.status_code == 200
        job_id = response.json()["job_id"]
        result = _wait_for_enhance_job(client, job_id, timeout=30)

    assert result["status"] == "done", result
    assert result["progress"] == 100
    output_name = result["result_url"].rsplit("/", 1)[-1]
    output_path = os.path.join(get_settings().temp_storage_dir, output_name)
    assert os.path.exists(output_path)
    assert os.path.getsize(output_path) > 1000


# ── Worker tests ───────────────────────────────────────────────────────────

def test_enhance_missing_backend_marks_failed(client):
    """If no Real-ESRGAN backend is ready, job fails with a clear error."""
    with patch("app.services.enhance_worker._resolve_real_esrgan_bin", return_value=None):
        with patch("app.services.enhance_worker._resolve_real_esrgan_python", return_value=None):
            with patch("app.services.enhance_worker._resolve_real_esrgan_model_path", return_value=None):
                r = client.post(
                    "/enhance-video",
                    files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
                    headers=AUTH,
                )
                assert r.status_code == 200
                job_id = r.json()["job_id"]

                result = _wait_for_enhance_job(client, job_id, timeout=10)

    assert result["status"] == "failed"
    assert "Real-ESRGAN" in (result.get("error") or "")
    assert "brew install" not in (result.get("error") or "")


def test_enhance_uses_python_backend_when_ncnn_crashes(client):
    """If ncnn segfaults, auto mode should use the Python Real-ESRGAN backend."""
    python_invocations: list[dict] = []

    class NcnnCrashPythonSuccessPopen:
        def __init__(self, cmd, stdout=None, stderr=None, cwd=None, env=None, **kwargs):
            self.stdout = iter([])
            self.stderr = iter([])
            self.returncode = 0
            cmd_list = list(cmd)

            if any(str(part).endswith("realesrgan-ncnn-vulkan") for part in cmd_list):
                self.returncode = -11
                return

            if str(cmd_list[0]) == "/tmp/realesrgan-python":
                python_invocations.append({"cmd": cmd_list, "cwd": cwd, "env": env})
                input_dir = cmd_list[cmd_list.index("--input") + 1]
                output_dir = cmd_list[cmd_list.index("--output") + 1]
                os.makedirs(output_dir, exist_ok=True)
                for name in os.listdir(input_dir):
                    source = os.path.join(input_dir, name)
                    if os.path.isfile(source):
                        shutil.copy(source, os.path.join(output_dir, name))
                return

            if "-segment_time" in cmd_list:
                pat = cmd_list[-1]
                chunk_dir = os.path.dirname(pat)
                os.makedirs(chunk_dir, exist_ok=True)
                with open(os.path.join(chunk_dir, "chunk_0000.mp4"), "wb") as f:
                    f.write(b"fake-chunk" * 200)
                return

            if str(cmd_list[-1]).endswith("%08d.png"):
                pat = cmd_list[-1]
                frames_dir = os.path.dirname(pat)
                os.makedirs(frames_dir, exist_ok=True)
                with open(os.path.join(frames_dir, "00000001.png"), "wb") as f:
                    f.write(b"fake-png" * 200)
                return

            out = cmd_list[-1]
            if not out.startswith("-") and "%" not in out and not out.endswith(".txt"):
                parent = os.path.dirname(out)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(out, "wb") as f:
                    f.write(b"fake-data" * 200)

        def wait(self):
            return self.returncode

        def kill(self):
            pass

    with patch("app.services.enhance_worker._resolve_real_esrgan_bin",
               return_value="/usr/local/bin/realesrgan-ncnn-vulkan"):
        with patch("app.services.enhance_worker._resolve_real_esrgan_python",
                   return_value="/tmp/realesrgan-python"):
            with patch("app.services.enhance_worker._resolve_real_esrgan_model_path",
                       return_value="/tmp/RealESRGAN_x4plus.pth"):
                with patch("app.services.enhance_worker._probe_video",
                           return_value=(30.0, 5.0, 640, 480)):
                    with patch("app.services.enhance_worker.subprocess.Popen",
                               NcnnCrashPythonSuccessPopen):
                        r = client.post(
                            "/enhance-video",
                            files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
                            headers=AUTH,
                        )
                        assert r.status_code == 200
                        result = _wait_for_enhance_job(client, r.json()["job_id"])

    assert result["status"] == "done", f"Expected Python fallback completion, got: {result}"
    assert result["result_url"] is not None
    assert python_invocations
    assert python_invocations[0]["cwd"].endswith("/backend")
    assert "PYTHONPATH" not in python_invocations[0]["env"]
    assert "backend/app/services" not in python_invocations[0]["cmd"][1]


def test_enhance_fails_when_ncnn_crashes_and_python_backend_missing(client):
    """If ncnn crashes and Python fallback is missing, job explains how to fix setup."""

    class CrashingNcnnPopen:
        def __init__(self, cmd, stdout=None, stderr=None, **kwargs):
            self.stdout = iter([])
            self.stderr = iter([])
            self.returncode = 0
            cmd_list = list(cmd)

            if any(str(part).endswith("realesrgan-ncnn-vulkan") for part in cmd_list):
                self.returncode = -11
                return

            if str(cmd_list[0]) == "/tmp/realesrgan-python":
                self.returncode = -11
                return

            if "-segment_time" in cmd_list:
                pat = cmd_list[-1]
                chunk_dir = os.path.dirname(pat)
                os.makedirs(chunk_dir, exist_ok=True)
                with open(os.path.join(chunk_dir, "chunk_0000.mp4"), "wb") as f:
                    f.write(b"fake-chunk" * 200)
                return

            if str(cmd_list[-1]).endswith("%08d.png"):
                pat = cmd_list[-1]
                frames_dir = os.path.dirname(pat)
                os.makedirs(frames_dir, exist_ok=True)
                with open(os.path.join(frames_dir, "00000001.png"), "wb") as f:
                    f.write(b"fake-png" * 200)
                return

            out = cmd_list[-1]
            if not out.startswith("-") and "%" not in out and not out.endswith(".txt"):
                parent = os.path.dirname(out)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(out, "wb") as f:
                    f.write(b"fake-data" * 200)

        def wait(self):
            return self.returncode

        def kill(self):
            pass

    with patch("app.services.enhance_worker._resolve_real_esrgan_bin",
               return_value="/usr/local/bin/realesrgan-ncnn-vulkan"):
        with patch("app.services.enhance_worker._resolve_real_esrgan_python", return_value=None):
            with patch("app.services.enhance_worker._resolve_real_esrgan_model_path", return_value=None):
                with patch("app.services.enhance_worker._probe_video",
                           return_value=(30.0, 5.0, 640, 480)):
                    with patch("app.services.enhance_worker.subprocess.Popen", CrashingNcnnPopen):
                        r = client.post(
                            "/enhance-video",
                            files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
                            headers=AUTH,
                        )
                        assert r.status_code == 200
                        result = _wait_for_enhance_job(client, r.json()["job_id"])

    assert result["status"] == "failed"
    assert "Python backend is not ready" in (result.get("error") or "")
    assert "setup.sh" in (result.get("error") or "")


def test_enhance_job_completes(client):
    """Full pipeline with mocked subprocess produces done status and result_url."""
    with patch("app.services.enhance_worker._resolve_real_esrgan_bin",
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


def test_enhance_retries_frame_by_frame_when_realesrgan_directory_mode_crashes(client):
    """Directory mode can crash on some GPUs; frame mode should still use Real-ESRGAN."""

    class DirectoryCrashPopen:
        def __init__(self, cmd, stdout=None, stderr=None, **kwargs):
            self.stdout = iter([])
            self.stderr = iter([])
            self.returncode = 0
            cmd_list = list(cmd)

            if any(str(part).endswith("realesrgan-ncnn-vulkan") for part in cmd_list):
                i_idx = cmd_list.index("-i")
                o_idx = cmd_list.index("-o")
                in_path = cmd_list[i_idx + 1]
                out_path = cmd_list[o_idx + 1]

                if os.path.isdir(in_path):
                    self.returncode = -11
                    return

                out_dir = os.path.dirname(out_path)
                os.makedirs(out_dir, exist_ok=True)
                shutil.copy(in_path, out_path)
                return

            if "-segment_time" in cmd_list:
                pat = cmd_list[-1]
                chunk_dir = os.path.dirname(pat)
                os.makedirs(chunk_dir, exist_ok=True)
                with open(os.path.join(chunk_dir, "chunk_0000.mp4"), "wb") as f:
                    f.write(b"fake-chunk" * 200)
                return

            if str(cmd_list[-1]).endswith("%08d.png"):
                pat = cmd_list[-1]
                frames_dir = os.path.dirname(pat)
                os.makedirs(frames_dir, exist_ok=True)
                with open(os.path.join(frames_dir, "00000001.png"), "wb") as f:
                    f.write(b"fake-png" * 200)
                return

            out = cmd_list[-1]
            if not out.startswith("-") and "%" not in out and not out.endswith(".txt"):
                parent = os.path.dirname(out)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(out, "wb") as f:
                    f.write(b"fake-data" * 200)

        def wait(self):
            return self.returncode

        def kill(self):
            pass

    with patch("app.services.enhance_worker._resolve_real_esrgan_bin",
               return_value="/usr/local/bin/realesrgan-ncnn-vulkan"):
        with patch("app.services.enhance_worker._probe_video",
                   return_value=(30.0, 5.0, 640, 480)):
            with patch("app.services.enhance_worker.subprocess.Popen", DirectoryCrashPopen):
                r = client.post(
                    "/enhance-video",
                    files=[("file", ("video.mp4", io.BytesIO(_fake_mp4()), "video/mp4"))],
                    headers=AUTH,
                )
                assert r.status_code == 200
                result = _wait_for_enhance_job(client, r.json()["job_id"])

    assert result["status"] == "done", f"Expected frame retry completion, got: {result}"
    assert result["result_url"] is not None


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

    with patch("app.services.enhance_worker._resolve_real_esrgan_bin",
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


def test_enhance_fails_when_realesrgan_crashes_in_directory_and_frame_modes(client):
    """If Real-ESRGAN cannot run, the job should fail instead of pretending AI worked."""

    class CrashingRealEsrganPopen:
        def __init__(self, cmd, stdout=None, stderr=None, **kwargs):
            self.stdout = iter([])
            self.stderr = iter([])
            self.returncode = 0
            cmd_list = list(cmd)

            if any(str(part).endswith("realesrgan-ncnn-vulkan") for part in cmd_list):
                self.returncode = -11
                return

            if str(cmd_list[0]) == "/tmp/realesrgan-python":
                self.returncode = -11
                return

            if "-segment_time" in cmd_list:
                pat = cmd_list[-1]
                chunk_dir = os.path.dirname(pat)
                os.makedirs(chunk_dir, exist_ok=True)
                with open(os.path.join(chunk_dir, "chunk_0000.mp4"), "wb") as f:
                    f.write(b"fake-chunk" * 200)
                return

            if str(cmd_list[-1]).endswith("%08d.png"):
                pat = cmd_list[-1]
                frames_dir = os.path.dirname(pat)
                os.makedirs(frames_dir, exist_ok=True)
                with open(os.path.join(frames_dir, "00000001.png"), "wb") as f:
                    f.write(b"fake-png" * 200)
                return

            out = cmd_list[-1]
            if not out.startswith("-") and "%" not in out and not out.endswith(".txt"):
                parent = os.path.dirname(out)
                if parent:
                    os.makedirs(parent, exist_ok=True)
                with open(out, "wb") as f:
                    f.write(b"fake-data" * 200)

        def wait(self):
            return self.returncode

        def kill(self):
            pass

    with patch("app.services.enhance_worker._resolve_real_esrgan_bin",
               return_value="/usr/local/bin/realesrgan-ncnn-vulkan"):
        with patch("app.services.enhance_worker._resolve_real_esrgan_python",
                   return_value="/tmp/realesrgan-python"):
            with patch("app.services.enhance_worker._resolve_real_esrgan_model_path",
                       return_value="/tmp/RealESRGAN_x4plus.pth"):
                with patch("app.services.enhance_worker._probe_video",
                           return_value=(30.0, 5.0, 640, 480)):
                    with patch("app.services.enhance_worker.subprocess.Popen", CrashingRealEsrganPopen):
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

    assert result["status"] == "failed", f"Expected Real-ESRGAN failure, got: {result}"
    assert "SIGSEGV" in (result.get("error") or "")


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
