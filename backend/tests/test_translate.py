"""End-to-end tests for translate video endpoints and worker."""
import io
import time
import pytest
from unittest.mock import patch, MagicMock, AsyncMock

AUTH = {"Authorization": "Bearer test-token"}

FAKE_SEGMENTS = [
    {"start": 0.0, "end": 2.5, "text": "안녕하세요"},
    {"start": 2.5, "end": 5.0, "text": "반갑습니다"},
]

FAKE_TRANSLATED_SRT = (
    "1\n00:00:00,000 --> 00:00:02,500\nHello\n\n"
    "2\n00:00:02,500 --> 00:00:05,000\nNice to meet you\n"
)


def _fake_mp4() -> bytes:
    return b"\x00" * 2048


def _fake_extract_audio(job, video_path, audio_path):
    """Stub that creates a dummy WAV so the worker sees the file exists."""
    with open(audio_path, "wb") as f:
        f.write(b"fake-wav-data" * 100)
    return True


def _make_fake_popen(progress_lines=None):
    """FakePopen for the subtitle-burn ffmpeg call."""
    if progress_lines is None:
        progress_lines = ["out_time=00:00:05.000000\n", "progress=end\n"]

    class FakePopen:
        def __init__(self, cmd, stdout=None, stderr=None, text=None,
                     encoding=None, errors=None, bufsize=None):
            with open(cmd[-1], "wb") as f:
                f.write(b"fake-burned-mp4" * 200)
            self.stdout = iter(progress_lines)
            self.stderr = iter([])
            self.returncode = 0

        def wait(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    return FakePopen


def _make_mocks():
    fake_adapter = MagicMock()
    fake_adapter.transcribe.return_value = FAKE_SEGMENTS

    fake_llm = MagicMock()
    fake_llm.execute_translate = AsyncMock(return_value=FAKE_TRANSLATED_SRT)
    return fake_adapter, fake_llm


# ── Route-level tests ──────────────────────────────────────────────────────

def test_translate_creates_job(client):
    r = client.post(
        "/translate-video",
        files={"file": ("test.mp4", io.BytesIO(_fake_mp4()), "video/mp4")},
        headers=AUTH,
    )
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "queued"
    assert "job_id" in data
    assert data["filename"] == "test.mp4"


def test_translate_no_file_returns_422(client):
    r = client.post("/translate-video", headers=AUTH)
    assert r.status_code == 422


def test_translate_job_not_found(client):
    r = client.get("/translate-jobs/nonexistent-job-id", headers=AUTH)
    assert r.status_code == 404


def test_cancel_translate_job_not_found(client):
    r = client.delete("/translate-jobs/nonexistent-job-id", headers=AUTH)
    assert r.status_code == 404


# ── Full pipeline tests ────────────────────────────────────────────────────

def test_translate_job_completes(client):
    """Full pipeline: audio extract → transcribe → translate → burn → done."""
    fake_adapter, fake_llm = _make_mocks()
    FakePopen = _make_fake_popen()

    with (
        patch("app.services.translate_worker._extract_audio", side_effect=_fake_extract_audio),
        patch("app.services.translate_worker.probe_duration", return_value=10.0),
        patch("app.services.translate_worker.subprocess.Popen", FakePopen),
        patch("app.state.transcription_adapter", fake_adapter),
        patch("app.state.translate_llm_manager", fake_llm),
    ):
        r = client.post(
            "/translate-video",
            files={"file": ("movie.mp4", io.BytesIO(_fake_mp4()), "video/mp4")},
            headers=AUTH,
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        deadline = time.time() + 15
        result = {}
        while time.time() < deadline:
            r2 = client.get(f"/translate-jobs/{job_id}", headers=AUTH)
            result = r2.json()
            if result["status"] in ("done", "failed"):
                break
            time.sleep(0.1)

    assert result["status"] == "done", f"Expected done, got: {result}"
    assert result["result_url"] is not None
    assert result["overall_progress"] == 100


def test_translate_all_pipeline_stages_executed(client):
    """Verifies every pipeline stage ran: extract, transcribe, translate, burn."""
    fake_adapter, fake_llm = _make_mocks()
    extract_calls = []

    def tracking_extract(job, video_path, audio_path):
        extract_calls.append(audio_path)
        with open(audio_path, "wb") as f:
            f.write(b"fake-wav" * 100)
        return True

    FakePopen = _make_fake_popen()

    with (
        patch("app.services.translate_worker._extract_audio", side_effect=tracking_extract),
        patch("app.services.translate_worker.probe_duration", return_value=10.0),
        patch("app.services.translate_worker.subprocess.Popen", FakePopen),
        patch("app.state.transcription_adapter", fake_adapter),
        patch("app.state.translate_llm_manager", fake_llm),
    ):
        r = client.post(
            "/translate-video",
            files={"file": ("movie.mp4", io.BytesIO(_fake_mp4()), "video/mp4")},
            headers=AUTH,
        )
        job_id = r.json()["job_id"]

        deadline = time.time() + 15
        result = {}
        while time.time() < deadline:
            r2 = client.get(f"/translate-jobs/{job_id}", headers=AUTH)
            result = r2.json()
            if result["status"] in ("done", "failed"):
                break
            time.sleep(0.05)

    assert result["status"] == "done", f"Job did not complete: {result}"
    assert len(extract_calls) == 1, "Audio extraction did not run"
    assert fake_adapter.transcribe.call_count == 1, "Transcription did not run"
    assert fake_llm.execute_translate.call_count == 1, "LLM translation did not run"


def test_translate_transcription_failure_marks_failed(client):
    """If transcription raises, the job ends in failed with a descriptive error."""

    def fake_extract_audio_ok(job, video_path, audio_path):
        with open(audio_path, "wb") as f:
            f.write(b"fake-wav" * 100)
        return True

    fake_adapter = MagicMock()
    fake_adapter.transcribe.side_effect = RuntimeError("Whisper OOM")
    fake_llm = MagicMock()

    with (
        patch("app.services.translate_worker._extract_audio", side_effect=fake_extract_audio_ok),
        patch("app.services.translate_worker.probe_duration", return_value=10.0),
        patch("app.state.transcription_adapter", fake_adapter),
        patch("app.state.translate_llm_manager", fake_llm),
    ):
        r = client.post(
            "/translate-video",
            files={"file": ("movie.mp4", io.BytesIO(_fake_mp4()), "video/mp4")},
            headers=AUTH,
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        deadline = time.time() + 10
        result = {}
        while time.time() < deadline:
            r2 = client.get(f"/translate-jobs/{job_id}", headers=AUTH)
            result = r2.json()
            if result["status"] in ("done", "failed"):
                break
            time.sleep(0.1)

    assert result["status"] == "failed"
    assert "Transcription failed" in (result.get("error") or "")


def test_translate_audio_extraction_failure_marks_failed(client):
    """If audio extraction fails, the job ends in failed."""
    fake_adapter, fake_llm = _make_mocks()

    def fake_extract_fail(job, video_path, audio_path):
        return False  # Simulate extraction failure

    with (
        patch("app.services.translate_worker._extract_audio", side_effect=fake_extract_fail),
        patch("app.state.transcription_adapter", fake_adapter),
        patch("app.state.translate_llm_manager", fake_llm),
    ):
        r = client.post(
            "/translate-video",
            files={"file": ("movie.mp4", io.BytesIO(_fake_mp4()), "video/mp4")},
            headers=AUTH,
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        deadline = time.time() + 10
        result = {}
        while time.time() < deadline:
            r2 = client.get(f"/translate-jobs/{job_id}", headers=AUTH)
            result = r2.json()
            if result["status"] in ("done", "failed"):
                break
            time.sleep(0.1)

    assert result["status"] == "failed"
    assert "Audio extraction failed" in (result.get("error") or "")


def test_translate_cancel_job(client):
    """Cancelling a queued/processing job marks it cancelled."""
    r = client.post(
        "/translate-video",
        files={"file": ("movie.mp4", io.BytesIO(_fake_mp4()), "video/mp4")},
        headers=AUTH,
    )
    assert r.status_code == 200
    job_id = r.json()["job_id"]

    r2 = client.delete(f"/translate-jobs/{job_id}", headers=AUTH)
    # 204 = cancelled; 404 = worker already finished (race condition)
    assert r2.status_code in (204, 404)

    if r2.status_code == 204:
        r3 = client.get(f"/translate-jobs/{job_id}", headers=AUTH)
        assert r3.json()["status"] == "cancelled"


def test_translate_subtitle_burn_failure_marks_failed(client):
    """If ffmpeg subtitle burn fails, the job ends in failed."""
    fake_adapter, fake_llm = _make_mocks()

    class FailingPopen:
        def __init__(self, cmd, stdout=None, stderr=None, text=None,
                     encoding=None, errors=None, bufsize=None):
            self.stdout = iter([])
            self.stderr = iter(["ffmpeg error\n"])
            self.returncode = 1

        def wait(self):
            return self.returncode

        def kill(self):
            self.returncode = -9

    with (
        patch("app.services.translate_worker._extract_audio", side_effect=_fake_extract_audio),
        patch("app.services.translate_worker.probe_duration", return_value=10.0),
        patch("app.services.translate_worker.subprocess.Popen", FailingPopen),
        patch("app.state.transcription_adapter", fake_adapter),
        patch("app.state.translate_llm_manager", fake_llm),
    ):
        r = client.post(
            "/translate-video",
            files={"file": ("movie.mp4", io.BytesIO(_fake_mp4()), "video/mp4")},
            headers=AUTH,
        )
        assert r.status_code == 200
        job_id = r.json()["job_id"]

        deadline = time.time() + 15
        result = {}
        while time.time() < deadline:
            r2 = client.get(f"/translate-jobs/{job_id}", headers=AUTH)
            result = r2.json()
            if result["status"] in ("done", "failed"):
                break
            time.sleep(0.1)

    assert result["status"] == "failed"
