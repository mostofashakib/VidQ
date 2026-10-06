"""Album links fan out into one queue job per video, each fetched with the album page as Referer."""

import pytest

from app.services.scraper import pipeline
from app.services.scraper.album import Album, AlbumItem

AUTH = {"Authorization": "Bearer test-token"}
PAGE = "https://albums.example/a/AbC123"
ALBUM = Album(
    title="Beach Trip",
    page_url=PAGE,
    items=[
        AlbumItem(url="https://v.example/one.mp4", thumbnail="https://s.example/one.jpg", title="Beach Trip (1/2)"),
        AlbumItem(url="https://v.example/two.mp4", thumbnail="https://s.example/two.jpg", title="Beach Trip (2/2)"),
    ],
)


# ── Pipeline ──────────────────────────────────────────────────────────────────

@pytest.fixture
def direct_file_download(monkeypatch):
    captured = {}

    async def detect(url, user_agent):
        return url, ""

    async def download(src, referer, **kwargs):
        captured.update(src=src, referer=referer)
        return "http://testserver/temp_storage/one.mp4"

    monkeypatch.setattr(pipeline, "_detect_direct_video_embed", detect)
    monkeypatch.setattr(pipeline, "_download_embed_video", download)
    return captured


@pytest.mark.asyncio
async def test_direct_file_is_downloaded_with_the_album_page_as_referer(direct_file_download):
    result = await pipeline.run_extraction("https://v.example/one.mp4", "UA", referer=PAGE)

    assert direct_file_download == {"src": "https://v.example/one.mp4", "referer": PAGE}
    assert result[4] == "http://testserver/temp_storage/one.mp4"


@pytest.mark.asyncio
async def test_direct_file_without_referer_uses_its_own_url(direct_file_download):
    await pipeline.run_extraction("https://v.example/one.mp4", "UA")

    assert direct_file_download["referer"] == "https://v.example/one.mp4"


# ── Queue ─────────────────────────────────────────────────────────────────────

def test_album_job_passes_referer_and_keeps_its_title_and_thumbnail(monkeypatch, db_session):
    from app.services import queue as queue_module

    seen = {}

    async def fake_run_extraction(**kwargs):
        seen.update(kwargs)
        return "", "", [kwargs["url"]], "", "http://testserver/temp_storage/album-two.mp4"

    monkeypatch.setattr("app.services.scraper.run_extraction", fake_run_extraction)

    video_queue = queue_module.VideoQueue(max_workers=1)
    job = queue_module.RecordingJob(
        job_id="job-album-item",
        url="https://v.example/two.mp4",
        category="test",
        token="token",
        referer=PAGE,
        title_hint="Beach Trip (2/2)",
        thumbnail_hint="https://s.example/two.jpg",
    )
    video_queue._jobs[job.job_id] = job

    video_queue._process(job)

    assert job.status == queue_module.JobStatus.DONE
    assert seen["referer"] == PAGE
    assert job.result["title"] == "Beach Trip (2/2)"
    assert job.result["thumbnail"] == "https://s.example/two.jpg"


# ── Endpoints ─────────────────────────────────────────────────────────────────

@pytest.fixture
def idle_workers(monkeypatch):
    """Enqueue without running any extraction."""
    from app.services import queue as queue_module

    monkeypatch.setattr(queue_module.VideoQueue, "_process", lambda self, job, thread_index=0: None)
    return queue_module.video_queue


@pytest.mark.parametrize("endpoint", ["/queue", "/extract-video"])
def test_album_link_enqueues_one_job_per_video(client, monkeypatch, idle_workers, endpoint):
    monkeypatch.setattr("app.routers.video.fetch_album", lambda url, user_agent: ALBUM)

    r = client.post(endpoint, json={"url": PAGE, "category": "trips"}, headers=AUTH)

    assert r.status_code == 200
    data = r.json()
    assert [j["title"] for j in data["jobs"]] == ["Beach Trip (1/2)", "Beach Trip (2/2)"]
    assert data["job_id"] == data["jobs"][0]["job_id"]
    jobs = [idle_workers.get(j["job_id"]) for j in data["jobs"]]
    assert [job.url for job in jobs] == ["https://v.example/one.mp4", "https://v.example/two.mp4"]
    assert {job.referer for job in jobs} == {PAGE}
    assert [job.thumbnail_hint for job in jobs] == ["https://s.example/one.jpg", "https://s.example/two.jpg"]
    assert {job.category for job in jobs} == {"trips"}


def test_regular_link_enqueues_a_single_job(client, monkeypatch, idle_workers):
    monkeypatch.setattr("app.routers.video.fetch_album", lambda url, user_agent: None)

    r = client.post("/queue", json={"url": "https://example.com/video"}, headers=AUTH)

    data = r.json()
    assert len(data["jobs"]) == 1
    assert data["jobs"][0]["job_id"] == data["job_id"]
    job = idle_workers.get(data["job_id"])
    assert job.url == "https://example.com/video"
    assert job.referer is None


def test_album_videos_on_unsafe_hosts_are_skipped(client, monkeypatch, idle_workers):
    unsafe = Album(title="T", page_url=PAGE, items=[
        AlbumItem(url="http://10.0.0.5/one.mp4", thumbnail="", title="T (1/2)"),
        AlbumItem(url="https://v.example/two.mp4", thumbnail="", title="T (2/2)"),
    ])
    monkeypatch.setattr("app.routers.video.fetch_album", lambda url, user_agent: unsafe)

    data = client.post("/queue", json={"url": PAGE}, headers=AUTH).json()

    assert [j["title"] for j in data["jobs"]] == ["T (2/2)"]


def test_album_with_no_safe_videos_falls_back_to_a_single_page_job(client, monkeypatch, idle_workers):
    unsafe = Album(title="T", page_url=PAGE, items=[
        AlbumItem(url="http://10.0.0.5/one.mp4", thumbnail="", title="T (1/2)"),
        AlbumItem(url="http://192.168.1.2/two.mp4", thumbnail="", title="T (2/2)"),
    ])
    monkeypatch.setattr("app.routers.video.fetch_album", lambda url, user_agent: unsafe)

    data = client.post("/queue", json={"url": PAGE}, headers=AUTH).json()

    assert len(data["jobs"]) == 1
    assert idle_workers.get(data["job_id"]).url == PAGE
