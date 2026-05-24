"""Video CRUD endpoint integration tests.

Title and duration are always supplied so extract_title_and_duration()
(which makes a real network request) is never called during tests.
"""
import pytest
import os

AUTH = {"Authorization": "Bearer test-token"}

VIDEO_PAYLOAD = {
    "url": "https://example.com/video.mp4",
    "category": "test",
    "title": "Test Video",
    "duration": 120.0,
}


@pytest.fixture(autouse=True)
def clean_videos(db_session):
    """Wipe the videos table before each test for isolation."""
    from app.db import Video
    db_session.query(Video).delete()
    db_session.commit()
    yield


def test_add_video(client):
    r = client.post("/videos", json=VIDEO_PAYLOAD, headers=AUTH)
    assert r.status_code == 200
    data = r.json()
    assert data["url"] == VIDEO_PAYLOAD["url"]
    assert data["category"] == VIDEO_PAYLOAD["category"]
    assert data["title"] == VIDEO_PAYLOAD["title"]
    assert data["duration"] == VIDEO_PAYLOAD["duration"]
    assert "id" in data
    assert "created_at" in data


def test_add_video_normalises_url_to_lowercase(client):
    payload = {**VIDEO_PAYLOAD, "url": "https://Example.COM/Video.MP4"}
    r = client.post("/videos", json=payload, headers=AUTH)
    assert r.status_code == 200
    assert r.json()["url"] == "https://example.com/video.mp4"


def test_duplicate_url_returns_409(client):
    client.post("/videos", json=VIDEO_PAYLOAD, headers=AUTH)
    r = client.post("/videos", json=VIDEO_PAYLOAD, headers=AUTH)
    assert r.status_code == 409


def test_duplicate_title_returns_409(client):
    client.post("/videos", json=VIDEO_PAYLOAD, headers=AUTH)
    r = client.post("/videos", json={**VIDEO_PAYLOAD, "url": "https://other.com/v.mp4"}, headers=AUTH)
    assert r.status_code == 409


def test_list_videos_empty(client):
    r = client.get("/videos", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == []


def test_list_videos_returns_added(client):
    client.post("/videos", json=VIDEO_PAYLOAD, headers=AUTH)
    r = client.get("/videos", headers=AUTH)
    assert r.status_code == 200
    items = r.json()
    assert len(items) == 1
    assert items[0]["title"] == "Test Video"


def test_list_videos_category_filter(client):
    client.post("/videos", json=VIDEO_PAYLOAD, headers=AUTH)
    client.post("/videos", json={**VIDEO_PAYLOAD, "url": "https://other.com/b.mp4", "title": "Other", "category": "other"}, headers=AUTH)

    r = client.get("/videos?category=test", headers=AUTH)
    assert all(v["category"] == "test" for v in r.json())

    r = client.get("/videos?category=other", headers=AUTH)
    assert all(v["category"] == "other" for v in r.json())


def test_list_categories(client):
    client.post("/videos", json=VIDEO_PAYLOAD, headers=AUTH)
    client.post("/videos", json={**VIDEO_PAYLOAD, "url": "https://other.com/c.mp4", "title": "C", "category": "cats"}, headers=AUTH)

    r = client.get("/videos/categories", headers=AUTH)
    assert r.status_code == 200
    cats = r.json()
    assert "test" in cats
    assert "cats" in cats


def test_delete_video(client):
    add = client.post("/videos", json=VIDEO_PAYLOAD, headers=AUTH)
    video_id = add.json()["id"]

    r = client.delete(f"/videos/{video_id}", headers=AUTH)
    assert r.status_code == 204

    r = client.get("/videos", headers=AUTH)
    assert r.json() == []


def test_delete_nonexistent_video_returns_404(client):
    r = client.delete("/videos/99999", headers=AUTH)
    assert r.status_code == 404


def test_list_videos_pagination(client):
    for i in range(5):
        client.post("/videos", json={**VIDEO_PAYLOAD, "url": f"https://example.com/{i}.mp4", "title": f"Video {i}"}, headers=AUTH)

    r = client.get("/videos?skip=0&limit=3", headers=AUTH)
    assert len(r.json()) == 3

    r = client.get("/videos?skip=3&limit=3", headers=AUTH)
    assert len(r.json()) == 2


def test_add_video_rejects_localhost_url(client):
    r = client.post("/videos", json={**VIDEO_PAYLOAD, "url": "http://localhost:8080/internal"}, headers=AUTH)
    assert r.status_code == 400


def test_add_video_rejects_private_ip(client):
    r = client.post("/videos", json={**VIDEO_PAYLOAD, "url": "http://192.168.1.1/video"}, headers=AUTH)
    assert r.status_code == 400


def test_list_videos_excludes_uploads(client, db_session):
    from app.db import Video
    from datetime import datetime
    upload = Video(
        url="http://localhost:8000/temp_storage/uploaded.mp4",
        category="test",
        title="Uploaded",
        duration=10.0,
        source="upload",
        created_at=datetime.utcnow(),
    )
    db_session.add(upload)
    db_session.commit()

    r = client.get("/videos", headers=AUTH)
    assert r.status_code == 200
    titles = [v["title"] for v in r.json()]
    assert "Uploaded" not in titles


def test_list_categories_excludes_uploads(client, db_session):
    from app.db import Video
    from datetime import datetime
    upload = Video(
        url="http://localhost:8000/temp_storage/cat_upload.mp4",
        category="upload-only-cat",
        title="Upload Cat",
        duration=5.0,
        source="upload",
        created_at=datetime.utcnow(),
    )
    db_session.add(upload)
    db_session.commit()

    r = client.get("/videos/categories", headers=AUTH)
    assert r.status_code == 200
    assert "upload-only-cat" not in r.json()


def test_delete_upload_removes_file(client, db_session, tmp_path):
    import shutil
    from app.db import Video
    from datetime import datetime
    from app.config import get_settings

    settings = get_settings()
    # Create a real file in temp_storage
    fake_file = tmp_path / "fake_upload.mp4"
    fake_file.write_bytes(b"fake video content")
    dest = os.path.join(settings.temp_storage_dir, "fake_upload.mp4")
    shutil.copy(str(fake_file), dest)

    video_url = f"{settings.base_url}/temp_storage/fake_upload.mp4"
    upload = Video(
        url=video_url.lower(),
        category="test",
        title="Fake Upload",
        duration=5.0,
        source="upload",
        created_at=datetime.utcnow(),
    )
    db_session.add(upload)
    db_session.commit()
    video_id = upload.id

    r = client.delete(f"/videos/{video_id}", headers=AUTH)
    assert r.status_code == 204
    assert not os.path.exists(dest)
