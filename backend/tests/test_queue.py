"""Queue endpoint integration tests (no actual video processing)."""
import threading
import time

AUTH = {"Authorization": "Bearer test-token"}


def test_enqueue_returns_job_id(client):
    r = client.post("/queue", json={"url": "https://example.com/video", "category": "test"}, headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    assert data["status"] in ("queued", "processing")
    assert "queue_position" in data


def test_get_queue_status(client):
    enqueue = client.post("/queue", json={"url": "https://example.com/video", "category": "test"}, headers=AUTH)
    job_id = enqueue.json()["job_id"]

    r = client.get(f"/queue/{job_id}", headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["job_id"] == job_id
    assert data["status"] in ("queued", "processing", "done", "failed", "cancelled")


def test_get_queue_status_unknown_job_returns_404(client):
    r = client.get("/queue/nonexistent123abc", headers=AUTH)
    assert r.status_code == 404


def test_cancel_job(client):
    enqueue = client.post("/queue", json={"url": "https://example.com/video2", "category": "test"}, headers=AUTH)
    job_id = enqueue.json()["job_id"]

    r = client.delete(f"/queue/{job_id}", headers=AUTH)
    assert r.status_code == 200

    deadline = time.time() + 5
    status_data = {}
    while time.time() < deadline:
        status = client.get(f"/queue/{job_id}", headers=AUTH)
        status_data = status.json()
        if status_data["status"] == "cancelled":
            break
        time.sleep(0.05)

    assert status_data["status"] == "cancelled"


def test_cancel_unknown_job_returns_404(client):
    r = client.delete("/queue/nonexistent456xyz", headers=AUTH)
    assert r.status_code == 404


def test_enqueue_rejects_localhost_url(client):
    r = client.post("/queue", json={"url": "http://localhost:9999/video", "category": "test"}, headers=AUTH)
    assert r.status_code == 400


def test_video_queue_processes_up_to_worker_cap_and_queues_extra(monkeypatch):
    from app.services import queue as queue_module

    started: list[str] = []
    started_event = threading.Event()
    release_event = threading.Event()
    started_lock = threading.Lock()

    def fake_process(self, job, thread_index=0):
        with started_lock:
            started.append(job.job_id)
            if len(started) == 2:
                started_event.set()
        job.status = queue_module.JobStatus.PROCESSING
        release_event.wait(timeout=2)
        job.status = queue_module.JobStatus.DONE

    monkeypatch.setattr(queue_module.VideoQueue, "_process", fake_process)

    video_queue = queue_module.VideoQueue(max_workers=2)
    jobs = [
        video_queue.enqueue(f"https://example.com/video-{index}", "test", "token")
        for index in range(3)
    ]

    assert started_event.wait(timeout=2)
    assert sorted(job.job_id for job in jobs[:2]) == sorted(started)
    assert jobs[2].status == queue_module.JobStatus.QUEUED
    assert video_queue.position(jobs[2].job_id) == 0

    release_event.set()

    deadline = time.time() + 2
    while time.time() < deadline:
        if jobs[2].status != queue_module.JobStatus.QUEUED:
            break
        time.sleep(0.01)

    assert jobs[2].status in (queue_module.JobStatus.PROCESSING, queue_module.JobStatus.DONE)


def test_queue_falls_back_when_llm_metadata_fails(monkeypatch, db_session):
    from app.services import queue as queue_module

    async def fake_run_extraction(**kwargs):
        return (
            "<html><title>Ignored</title></html>",
            "screenshot",
            [],
            "thumb.jpg",
            "http://testserver/temp_storage/recorded.mp4",
        )

    async def fake_llm_metadata(*args, **kwargs):
        raise RuntimeError("LLM unavailable")

    monkeypatch.setattr("app.services.scraper.run_extraction", fake_run_extraction)
    monkeypatch.setattr("app.routers.video.call_llm_with_html_and_screenshot", fake_llm_metadata)

    video_queue = queue_module.VideoQueue(max_workers=1)
    job = queue_module.RecordingJob(
        job_id="job-llm-fallback",
        url="https://example.com/my-video-page",
        category="test",
        token="token",
    )
    video_queue._jobs[job.job_id] = job

    video_queue._process(job)

    assert job.status == queue_module.JobStatus.DONE
    assert job.result["video_url"] == "http://testserver/temp_storage/recorded.mp4"
    assert job.result["thumbnail"] == "thumb.jpg"
    assert job.result["title"] == "my video page"
    assert job.result["db_id"] is not None


def test_queue_marks_job_failed_when_recording_is_stuck(monkeypatch):
    from app.services import queue as queue_module

    async def fake_run_extraction(**kwargs):
        raise RuntimeError("Video failed to download: MediaRecorder output was stuck or blank.")

    monkeypatch.setattr("app.services.scraper.run_extraction", fake_run_extraction)

    video_queue = queue_module.VideoQueue(max_workers=1)
    job = queue_module.RecordingJob(
        job_id="job-stuck-recording",
        url="https://example.com/stuck-video",
        category="test",
        token="token",
    )
    video_queue._jobs[job.job_id] = job

    video_queue._process(job)

    assert job.status == queue_module.JobStatus.FAILED
    assert "stuck or blank" in job.error
