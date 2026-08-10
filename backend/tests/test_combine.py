"""End-to-end tests for combine video endpoints and worker."""
import io
import os
import subprocess
import time
import pytest
from unittest.mock import patch

import imageio_ffmpeg

from app.config import get_settings
from app.services.ffmpeg_utils import probe_duration
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


def _make_silent_video_bytes(tmp_path, filename: str, *, size: str) -> bytes:
    """A clip with no audio track at all."""
    path = tmp_path / filename
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    subprocess.run(
        [
            ffmpeg_exe, "-y",
            "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30:duration=2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p",
            str(path),
        ],
        check=True, capture_output=True, text=True,
    )
    return path.read_bytes()


def _make_varying_params_video_bytes(tmp_path, filename: str) -> bytes:
    """A clip whose resolution changes mid-stream while its header still advertises the
    first configuration. ffmpeg reconfigures the filter graph at the switch, which used
    to stall xfade or silently drop every frame after it."""
    ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    segments = []
    for index, size in enumerate(("1280x720", "640x360")):
        segment = tmp_path / f"{filename}.part{index}.ts"
        subprocess.run(
            [
                ffmpeg_exe, "-y",
                "-f", "lavfi", "-i", f"testsrc=size={size}:rate=30:duration=2",
                "-f", "lavfi", "-i", "sine=frequency=440:duration=2",
                "-c:v", "libx264", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-ar", "48000", "-ac", "2",
                str(segment),
            ],
            check=True, capture_output=True, text=True,
        )
        segments.append(segment)

    joined = tmp_path / f"{filename}.ts"
    joined.write_bytes(b"".join(segment.read_bytes() for segment in segments))

    path = tmp_path / filename
    subprocess.run(
        [ffmpeg_exe, "-y", "-i", str(joined), "-c", "copy", str(path)],
        check=True, capture_output=True, text=True,
    )
    return path.read_bytes()


def _run_combine(client, files: list[tuple[str, bytes]], timeout: float = 120) -> dict:
    r = client.post(
        "/combine-video",
        files=[("files", (name, io.BytesIO(data), "video/mp4")) for name, data in files],
        headers=AUTH,
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    deadline = time.time() + timeout
    result = {}
    while time.time() < deadline:
        result = client.get(f"/combine-jobs/{job_id}", headers=AUTH).json()
        if result["status"] in ("done", "failed"):
            break
        time.sleep(0.1)
    return result


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
        patch("app.services.combine_worker.probe_duration", return_value=10.0),
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
        patch("app.services.combine_worker.probe_duration", return_value=10.0),
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
        patch("app.services.combine_worker.probe_duration", return_value=5.0),
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


def test_combine_keeps_full_length_when_clip_params_change_mid_stream(client, tmp_path):
    """Regression: a clip that switches resolution part way through used to stall the
    xfade graph ("buffers queued" → best_input assertion) or drop every frame after the
    switch, silently truncating the merge. Every clip is normalized first now."""
    varying = _make_varying_params_video_bytes(tmp_path, "varying.mp4")
    steady = _make_test_video_bytes(tmp_path, "steady.mp4", size="1280x720", frequency=880)

    result = _run_combine(client, [("varying.mp4", varying), ("steady.mp4", steady)])

    assert result["status"] == "done", f"Expected done, got: {result}"
    output_path = os.path.join(
        get_settings().temp_storage_dir, result["result_url"].rsplit("/", 1)[-1]
    )
    assert probe_video_dimensions(output_path) == (1280, 720)
    # 4s of varying clip + 1s clip, less the 0.5s crossfade.
    assert probe_duration(output_path) == pytest.approx(4.5, abs=0.4)


def test_combine_handles_clip_without_audio(client, tmp_path):
    """A clip with no audio track leaves the xfade graph with no [n:a] pad to read from;
    normalization gives it a silent one."""
    silent = _make_silent_video_bytes(tmp_path, "silent.mp4", size="640x360")
    with_audio = _make_test_video_bytes(tmp_path, "sound.mp4", size="1280x720", frequency=440)

    result = _run_combine(client, [("silent.mp4", silent), ("sound.mp4", with_audio)])

    assert result["status"] == "done", f"Expected done, got: {result}"
    output_path = os.path.join(
        get_settings().temp_storage_dir, result["result_url"].rsplit("/", 1)[-1]
    )
    assert probe_video_dimensions(output_path) == (1280, 720)
    # 2s silent clip + 1s clip, less the 0.5s crossfade.
    assert probe_duration(output_path) == pytest.approx(2.5, abs=0.4)


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
        patch("app.services.combine_worker.probe_duration", return_value=10.0),
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
