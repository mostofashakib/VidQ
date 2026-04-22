"""Queue endpoint integration tests (no actual video processing)."""

AUTH = {"Authorization": "Bearer test-token"}


def test_enqueue_returns_job_id(client):
    r = client.post("/queue", json={"url": "https://example.com/video", "category": "test"}, headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert "job_id" in data
    assert data["status"] == "queued"
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


def test_cancel_queued_job(client):
    enqueue = client.post("/queue", json={"url": "https://example.com/video2", "category": "test"}, headers=AUTH)
    job_id = enqueue.json()["job_id"]

    r = client.delete(f"/queue/{job_id}", headers=AUTH)
    assert r.status_code == 200

    status = client.get(f"/queue/{job_id}", headers=AUTH)
    assert status.json()["status"] == "cancelled"


def test_cancel_unknown_job_returns_404(client):
    r = client.delete("/queue/nonexistent456xyz", headers=AUTH)
    assert r.status_code == 404


def test_enqueue_rejects_localhost_url(client):
    r = client.post("/queue", json={"url": "http://localhost:9999/video", "category": "test"}, headers=AUTH)
    assert r.status_code == 400
