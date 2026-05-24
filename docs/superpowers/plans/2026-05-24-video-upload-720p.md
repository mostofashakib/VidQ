# Video Upload & 720p Auto-Scale Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a `/upload` page where users can upload a local video file, have it auto-scaled to 720p via the existing `ensure_min_quality()`, and manage it (download/delete) independently from the main video library.

**Architecture:** A `source` column is added to the existing `Video` DB table to distinguish uploaded videos (`"upload"`) from URL-sourced ones (`"url"`). Two new backend endpoints handle upload and listing. The main page query filters out uploads. A new Next.js page at `/upload` mirrors the home page's glass-panel aesthetic with a drag-and-drop file zone and a video grid.

**Tech Stack:** FastAPI (UploadFile/Form multipart), SQLAlchemy, ffmpeg via imageio-ffmpeg (`ensure_min_quality`), Next.js 14 App Router, TypeScript, Tailwind CSS, shadcn/ui.

---

## File Map

| File | Action | Responsibility |
|------|--------|----------------|
| `backend/app/db.py` | Modify | Add `source` column to `Video` model |
| `backend/app/models.py` | Modify | Add `source` field to `VideoOut` |
| `backend/app/routers/video.py` | Modify | Filter `GET /videos` + `GET /videos/categories`; update `DELETE` to remove files for uploads |
| `backend/app/routers/upload.py` | Create | `POST /upload-video` and `GET /upload-videos` endpoints |
| `backend/app/main.py` | Modify | Register upload router |
| `backend/tests/test_upload.py` | Create | Integration tests for upload endpoints |
| `backend/tests/test_videos.py` | Modify | Assert uploads excluded from `GET /videos` |
| `frontend/app/api.ts` | Modify | Add `uploadVideo` and `listUploadedVideos` functions |
| `frontend/app/upload/page.tsx` | Create | Upload page UI |
| `frontend/app/page.tsx` | Modify | Add "Upload Video" nav link in header |

---

## Task 1: Add `source` column to the Video DB model

**Files:**
- Modify: `backend/app/db.py`
- Modify: `backend/app/models.py`

- [ ] **Step 1: Add `source` column to the `Video` SQLAlchemy model in `db.py`**

Open `backend/app/db.py`. The file currently has these columns: `id`, `url`, `category`, `title`, `duration`, `thumbnail`, `created_at`. Add `source` after `thumbnail`:

```python
from sqlalchemy import create_engine, Column, Integer, String, DateTime, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker
from datetime import datetime
from app.config import get_settings

settings = get_settings()
SQLALCHEMY_DATABASE_URL = settings.database_url

engine = create_engine(SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Video(Base):
    __tablename__ = "videos"
    id = Column(Integer, primary_key=True, index=True)
    url = Column(String, nullable=False)
    category = Column(String, nullable=False)
    title = Column(String, nullable=True)
    duration = Column(Float, nullable=True)
    thumbnail = Column(String, nullable=True)
    source = Column(String, nullable=False, default="url")
    created_at = Column(DateTime, default=datetime.utcnow)

# For local dev: drop and recreate table to add 'source' column
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
```

- [ ] **Step 2: Add `source` field to `VideoOut` in `models.py`**

Open `backend/app/models.py` and add `source: str = "url"` to `VideoOut`:

```python
from pydantic import BaseModel
from typing import Optional
from datetime import datetime

class VideoCreate(BaseModel):
    url: str
    category: str
    title: Optional[str] = None
    duration: Optional[float] = None
    thumbnail: Optional[str] = None

class VideoOut(BaseModel):
    id: int
    url: str
    category: str
    title: Optional[str]
    duration: Optional[float]
    thumbnail: Optional[str]
    source: str
    created_at: datetime

    model_config = {"from_attributes": True}

class AuthRequest(BaseModel):
    password: str

class AuthResponse(BaseModel):
    token: str
```

- [ ] **Step 3: Run existing tests to confirm nothing is broken**

```bash
cd /Users/adibshakib/Coding/vidQ/backend
.venv/bin/pytest tests/test_videos.py -v
```

Expected: all tests pass (the `source` column has a default so existing test payloads don't need updating).

- [ ] **Step 4: Commit**

```bash
git add backend/app/db.py backend/app/models.py
git commit -m "feat: add source column to Video model for upload tracking"
```

---

## Task 2: Filter main page endpoints to exclude uploaded videos

**Files:**
- Modify: `backend/app/routers/video.py` (lines ~131–141)
- Modify: `backend/tests/test_videos.py`

- [ ] **Step 1: Write a failing test asserting uploads are excluded from `GET /videos`**

Add this test to `backend/tests/test_videos.py` (append after the last test):

```python
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
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/adibshakib/Coding/vidQ/backend
.venv/bin/pytest tests/test_videos.py::test_list_videos_excludes_uploads tests/test_videos.py::test_list_categories_excludes_uploads -v
```

Expected: FAIL — uploads currently appear in the listing.

- [ ] **Step 3: Add `source != "upload"` filter to `GET /videos` and `GET /videos/categories` in `video.py`**

In `backend/app/routers/video.py`, replace the two `list_*` endpoint bodies:

```python
@router.get("/videos", response_model=List[VideoOut])
def list_videos(category: Optional[str] = None, skip: int = 0, limit: int = 20, db: Session = Depends(get_db), token: str = Depends(verify_token)):
    query = db.query(Video).filter(Video.source != "upload")
    if category:
        query = query.filter(Video.category == category)
    return query.order_by(Video.created_at.desc()).offset(skip).limit(limit).all()

@router.get("/videos/categories", response_model=List[str])
def list_categories(db: Session = Depends(get_db), token: str = Depends(verify_token)):
    categories = db.query(Video.category).filter(Video.source != "upload").distinct().all()
    return [c[0] for c in categories]
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd /Users/adibshakib/Coding/vidQ/backend
.venv/bin/pytest tests/test_videos.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/video.py backend/tests/test_videos.py
git commit -m "feat: exclude upload-sourced videos from main library listing"
```

---

## Task 3: Update DELETE to clean up temp_storage files for uploads

**Files:**
- Modify: `backend/app/routers/video.py` (lines ~143–150)

- [ ] **Step 1: Write a failing test for upload file deletion**

Append to `backend/tests/test_videos.py`:

```python
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
```

Also add `import os` at the top of `test_videos.py` if not already present.

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd /Users/adibshakib/Coding/vidQ/backend
.venv/bin/pytest tests/test_videos.py::test_delete_upload_removes_file -v
```

Expected: FAIL — file still exists after delete.

- [ ] **Step 3: Update `delete_video` in `video.py` to remove the file for uploads**

Replace the `delete_video` endpoint body in `backend/app/routers/video.py`:

```python
@router.delete("/videos/{video_id}", status_code=204)
def delete_video(video_id: int, db: Session = Depends(get_db), token: str = Depends(verify_token)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")

    if video.source == "upload":
        settings = get_settings()
        filename = video.url.rstrip("/").split("/")[-1].split("?")[0]
        filepath = os.path.join(settings.temp_storage_dir, filename)
        if os.path.exists(filepath):
            try:
                os.remove(filepath)
            except Exception:
                logger.warning(f"Could not delete upload file: {filepath}")

    db.delete(video)
    db.commit()
    return None
```

- [ ] **Step 4: Run all video tests**

```bash
cd /Users/adibshakib/Coding/vidQ/backend
.venv/bin/pytest tests/test_videos.py -v
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add backend/app/routers/video.py backend/tests/test_videos.py
git commit -m "feat: delete temp_storage file when removing an uploaded video"
```

---

## Task 4: Create `POST /upload-video` and `GET /upload-videos` endpoints

**Files:**
- Create: `backend/app/routers/upload.py`
- Modify: `backend/app/main.py`
- Create: `backend/tests/test_upload.py`

- [ ] **Step 1: Write failing tests for the upload endpoints**

Create `backend/tests/test_upload.py`:

```python
"""Integration tests for the video upload endpoints."""
import io
import os
import pytest
from unittest.mock import patch

AUTH = {"Authorization": "Bearer test-token"}


@pytest.fixture(autouse=True)
def clean_videos(db_session):
    from app.db import Video
    db_session.query(Video).delete()
    db_session.commit()
    yield


def _fake_mp4_bytes() -> bytes:
    """Minimal valid-looking file content for tests (not a real video)."""
    return b"\x00" * 1024


def test_upload_video_returns_200(client):
    with patch("app.routers.upload.ensure_min_quality", side_effect=lambda p: p), \
         patch("app.routers.upload._probe_file_duration", return_value=30.0):
        r = client.post(
            "/upload-video",
            files={"file": ("test_video.mp4", io.BytesIO(_fake_mp4_bytes()), "video/mp4")},
            data={"category": "test"},
            headers=AUTH,
        )
    assert r.status_code == 200
    data = r.json()
    assert data["source"] == "upload"
    assert data["category"] == "test"
    assert data["title"] == "test_video"
    assert data["duration"] == 30.0
    assert "temp_storage" in data["url"]


def test_upload_video_missing_category_returns_422(client):
    r = client.post(
        "/upload-video",
        files={"file": ("video.mp4", io.BytesIO(_fake_mp4_bytes()), "video/mp4")},
        headers=AUTH,
    )
    assert r.status_code == 422


def test_list_upload_videos_empty(client):
    r = client.get("/upload-videos", headers=AUTH)
    assert r.status_code == 200
    assert r.json() == []


def test_list_upload_videos_returns_only_uploads(client, db_session):
    from app.db import Video
    from datetime import datetime
    from app.config import get_settings

    settings = get_settings()
    url_video = Video(url="https://example.com/url.mp4", category="test",
                      title="URL Video", source="url", created_at=datetime.utcnow())
    upload_video = Video(
        url=f"{settings.base_url}/temp_storage/up.mp4",
        category="test", title="Upload Video", source="upload",
        created_at=datetime.utcnow(),
    )
    db_session.add_all([url_video, upload_video])
    db_session.commit()

    r = client.get("/upload-videos", headers=AUTH)
    assert r.status_code == 200
    titles = [v["title"] for v in r.json()]
    assert "Upload Video" in titles
    assert "URL Video" not in titles
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd /Users/adibshakib/Coding/vidQ/backend
.venv/bin/pytest tests/test_upload.py -v
```

Expected: FAIL — endpoints do not exist yet.

- [ ] **Step 3: Create `backend/app/routers/upload.py`**

```python
import os
import shutil
import uuid
import logging

from fastapi import APIRouter, Depends, File, Form, UploadFile
from sqlalchemy.orm import Session
from typing import List

from app.config import get_settings
from app.db import get_db, Video
from app.models import VideoOut
from app.routers.auth import verify_token
from app.services.video_utils import ensure_min_quality
from app.services.scraper.media import _probe_file_duration

logger = logging.getLogger("UploadRouter")

router = APIRouter()


@router.post("/upload-video", response_model=VideoOut)
async def upload_video(
    file: UploadFile = File(...),
    category: str = Form(...),
    db: Session = Depends(get_db),
    token: str = Depends(verify_token),
):
    settings = get_settings()
    os.makedirs(settings.temp_storage_dir, exist_ok=True)

    original_name = file.filename or "video.mp4"
    ext = os.path.splitext(original_name)[1] or ".mp4"
    filename = f"{uuid.uuid4().hex}{ext}"
    file_path = os.path.join(settings.temp_storage_dir, filename)

    with open(file_path, "wb") as f:
        shutil.copyfileobj(file.file, f)

    final_path = ensure_min_quality(file_path)
    final_filename = os.path.basename(final_path)
    video_url = f"{settings.base_url}/temp_storage/{final_filename}"

    duration: float | None = None
    try:
        duration = _probe_file_duration(final_path)
    except Exception:
        pass

    title = os.path.splitext(original_name)[0]

    db_video = Video(
        url=video_url.strip().lower(),
        category=category,
        title=title,
        duration=duration,
        source="upload",
    )
    db.add(db_video)
    db.commit()
    db.refresh(db_video)
    logger.info(f"Uploaded video saved: id={db_video.id} path={final_path}")
    return db_video


@router.get("/upload-videos", response_model=List[VideoOut])
def list_upload_videos(
    db: Session = Depends(get_db),
    token: str = Depends(verify_token),
):
    return (
        db.query(Video)
        .filter(Video.source == "upload")
        .order_by(Video.created_at.desc())
        .all()
    )
```

- [ ] **Step 4: Register the upload router in `main.py`**

Open `backend/app/main.py` and add:

```python
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.logging_config import configure_logging
from app.routers.auth import router as auth_router
from app.routers.video import router as video_router
from app.routers.upload import router as upload_router
from app.config import get_settings

configure_logging()

app = FastAPI()

import os
settings = get_settings()
os.makedirs(settings.temp_storage_dir, exist_ok=True)

app.mount("/temp_storage", StaticFiles(directory=settings.temp_storage_dir), name="temp_storage")

if not settings.cors_origins:
    raise ValueError("Missing required environment variable: CORS_ORIGINS")

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(video_router)
app.include_router(upload_router)
```

- [ ] **Step 5: Run upload tests**

```bash
cd /Users/adibshakib/Coding/vidQ/backend
.venv/bin/pytest tests/test_upload.py -v
```

Expected: all tests pass.

- [ ] **Step 6: Run the full test suite**

```bash
cd /Users/adibshakib/Coding/vidQ/backend
.venv/bin/pytest -v
```

Expected: all tests pass.

- [ ] **Step 7: Commit**

```bash
git add backend/app/routers/upload.py backend/app/main.py backend/tests/test_upload.py
git commit -m "feat: add POST /upload-video and GET /upload-videos endpoints"
```

---

## Task 5: Add API functions to the frontend `api.ts`

**Files:**
- Modify: `frontend/app/api.ts`

- [ ] **Step 1: Add `uploadVideo` and `listUploadedVideos` to `api.ts`**

Open `frontend/app/api.ts` and append these two functions at the end of the file:

```typescript
export async function uploadVideo(
  token: string,
  file: File,
  category: string,
  onProgress?: (percent: number) => void
): Promise<{
  id: number; url: string; category: string; title?: string;
  duration?: number; thumbnail?: string; source: string; created_at: string;
}> {
  const form = new FormData();
  form.append("file", file);
  form.append("category", category);

  const res = await axios.post(`${API_URL}/upload-video`, form, {
    headers: {
      Authorization: `Bearer ${token}`,
      "Content-Type": "multipart/form-data",
    },
    onUploadProgress: (e) => {
      if (onProgress && e.total) {
        onProgress(Math.round((e.loaded / e.total) * 100));
      }
    },
  });
  return res.data;
}

export async function listUploadedVideos(token: string): Promise<
  Array<{
    id: number; url: string; category: string; title?: string;
    duration?: number; thumbnail?: string; source: string; created_at: string;
  }>
> {
  const res = await axios.get(`${API_URL}/upload-videos`, {
    headers: { Authorization: `Bearer ${token}` },
  });
  return res.data;
}
```

- [ ] **Step 2: Verify TypeScript compiles**

```bash
cd /Users/adibshakib/Coding/vidQ/frontend
npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/app/api.ts
git commit -m "feat: add uploadVideo and listUploadedVideos API functions"
```

---

## Task 6: Create the `/upload` page

**Files:**
- Create: `frontend/app/upload/page.tsx`

- [ ] **Step 1: Create the upload page**

Create `frontend/app/upload/page.tsx` with this full content:

```tsx
"use client";

import { useEffect, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useAuth } from "../auth-context";
import { uploadVideo, listUploadedVideos, deleteVideo, downloadVideo } from "../api";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import {
  Card, CardHeader, CardTitle, CardContent, CardFooter,
} from "@/components/ui/card";
import {
  Dialog, DialogContent, DialogHeader, DialogFooter, DialogTitle,
} from "@/components/ui/dialog";
import { Trash, Download, Check, X, Loader2, Upload } from "lucide-react";
import Link from "next/link";

interface UploadedVideo {
  id: number;
  url: string;
  category: string;
  title?: string;
  duration?: number;
  thumbnail?: string;
  source: string;
  created_at: string;
}

interface UploadJob {
  localId: string;
  filename: string;
  status: "uploading" | "done" | "failed";
  message: string;
  progress: number;
}

export default function UploadPage() {
  const { token, loading, authEnabled, logout } = useAuth();
  const router = useRouter();

  const [videos, setVideos] = useState<UploadedVideo[]>([]);
  const [jobs, setJobs] = useState<UploadJob[]>([]);
  const [category, setCategory] = useState("");
  const [error, setError] = useState("");
  const [deleteId, setDeleteId] = useState<number | null>(null);
  const [showDialog, setShowDialog] = useState(false);
  const [dragging, setDragging] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (!loading && !token) router.replace("/login");
  }, [token, loading, router]);

  useEffect(() => {
    if (!token) return;
    listUploadedVideos(token)
      .then(setVideos)
      .catch(() => setError("Failed to load uploaded videos"));
  }, [token]);

  function formatDuration(seconds?: number) {
    if (!seconds || isNaN(seconds)) return "Unknown";
    const m = Math.floor(seconds / 60);
    const s = Math.round(seconds % 60);
    return `${m}:${s.toString().padStart(2, "0")}`;
  }

  async function handleFiles(files: FileList | null) {
    if (!files || !files.length) return;
    if (!category.trim()) {
      setError("Please enter a category before uploading.");
      return;
    }
    setError("");

    for (const file of Array.from(files)) {
      const localId = `${Date.now()}-${Math.random().toString(36).slice(2)}`;
      setJobs((prev) => [
        ...prev,
        { localId, filename: file.name, status: "uploading", message: "Uploading and scaling to 720p…", progress: 0 },
      ]);

      try {
        const result = await uploadVideo(token!, file, category.trim(), (pct) => {
          setJobs((prev) =>
            prev.map((j) => j.localId === localId ? { ...j, progress: pct, message: pct < 100 ? `Uploading… ${pct}%` : "Scaling to 720p…" } : j)
          );
        });

        setJobs((prev) =>
          prev.map((j) => j.localId === localId ? { ...j, status: "done", message: "Done! Video scaled to 720p.", progress: 100 } : j)
        );
        setVideos((prev) => [result, ...prev]);
        setTimeout(() => {
          setJobs((prev) => prev.filter((j) => j.localId !== localId));
        }, 4000);
      } catch {
        setJobs((prev) =>
          prev.map((j) => j.localId === localId ? { ...j, status: "failed", message: "Upload failed." } : j)
        );
      }
    }
  }

  function onInputChange(e: React.ChangeEvent<HTMLInputElement>) {
    handleFiles(e.target.files);
    e.target.value = "";
  }

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setDragging(false);
    handleFiles(e.dataTransfer.files);
  }

  async function confirmDelete() {
    if (deleteId == null) return;
    try {
      await deleteVideo(token!, deleteId);
      setVideos((prev) => prev.filter((v) => v.id !== deleteId));
      setDeleteId(null);
      setShowDialog(false);
    } catch {
      setError("Failed to delete video");
    }
  }

  if (loading) {
    return <div className="min-h-screen flex items-center justify-center">Loading…</div>;
  }

  return (
    <div className="min-h-screen text-white pb-20">
      {/* Header */}
      <div className="flex justify-between items-center px-8 py-5 glass-panel sticky top-0 z-50 rounded-b-2xl mx-4 mb-10 shadow-xl shadow-indigo-500/10">
        <div className="flex items-center gap-4">
          <Link href="/">
            <span className="text-2xl font-bold bg-clip-text text-transparent bg-linear-to-r from-indigo-400 to-purple-400 cursor-pointer">
              VidQ
            </span>
          </Link>
          <span className="text-gray-500 text-sm hidden sm:inline">/ Upload</span>
        </div>
        <div className="flex items-center gap-3">
          <Link href="/">
            <Button variant="outline" className="border-white/10 bg-transparent hover:bg-white/10 hover:text-white transition-all rounded-xl text-gray-200 text-sm">
              ← Library
            </Button>
          </Link>
          {authEnabled && (
            <Button variant="outline" onClick={logout} className="border-white/10 bg-transparent hover:bg-white/10 hover:text-white transition-all rounded-xl text-gray-200">
              Logout
            </Button>
          )}
        </div>
      </div>

      <div className="max-w-6xl mx-auto px-4 sm:px-6">
        {/* Upload zone */}
        <div className="glass-panel p-6 md:p-8 rounded-4xl mb-8 shadow-2xl shadow-purple-500/5">
          <div className="flex flex-col sm:flex-row gap-4 mb-6">
            <Input
              placeholder="Category Name"
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="w-full sm:w-64 bg-white/5 border-white/10 focus-visible:ring-indigo-500 rounded-xl h-14 text-white placeholder:text-gray-400 px-5 text-base"
            />
          </div>

          {/* Drag-and-drop zone */}
          <div
            onDragOver={(e) => { e.preventDefault(); setDragging(true); }}
            onDragLeave={() => setDragging(false)}
            onDrop={onDrop}
            onClick={() => fileInputRef.current?.click()}
            className={`border-2 border-dashed rounded-3xl flex flex-col items-center justify-center gap-3 py-16 cursor-pointer transition-all duration-300 ${
              dragging
                ? "border-indigo-400 bg-indigo-500/10"
                : "border-white/10 hover:border-indigo-500/50 hover:bg-white/5"
            }`}
          >
            <div className="w-14 h-14 rounded-full bg-indigo-500/15 flex items-center justify-center">
              <Upload className="w-6 h-6 text-indigo-400" />
            </div>
            <p className="text-white font-medium">Drop a video file here</p>
            <p className="text-gray-400 text-sm">or click to browse — any resolution, auto-scaled to 720p</p>
            <input
              ref={fileInputRef}
              type="file"
              accept="video/*"
              multiple
              className="hidden"
              onChange={onInputChange}
            />
          </div>
        </div>

        {error && <div className="text-red-400 mb-4">{error}</div>}

        {/* Upload progress cards */}
        {jobs.length > 0 && (
          <div className="mb-10 space-y-2.5">
            {jobs.map((job) => (
              <div
                key={job.localId}
                className={`glass-panel px-5 py-4 rounded-2xl border flex items-center gap-4 transition-all ${
                  job.status === "done"
                    ? "border-green-500/25"
                    : job.status === "failed"
                    ? "border-red-500/25"
                    : "border-indigo-500/20"
                }`}
              >
                <div className="shrink-0">
                  {job.status === "done" ? (
                    <div className="w-8 h-8 rounded-full bg-green-500/15 flex items-center justify-center">
                      <Check className="w-4 h-4 text-green-400" />
                    </div>
                  ) : job.status === "failed" ? (
                    <div className="w-8 h-8 rounded-full bg-red-500/15 flex items-center justify-center">
                      <X className="w-4 h-4 text-red-400" />
                    </div>
                  ) : (
                    <div className="w-8 h-8 rounded-full bg-indigo-500/15 flex items-center justify-center">
                      <Loader2 className="w-4 h-4 text-indigo-400 animate-spin" />
                    </div>
                  )}
                </div>
                <div className="flex-1 min-w-0">
                  <p className="text-sm font-medium text-white truncate">{job.filename}</p>
                  <p className={`text-xs mt-0.5 ${job.status === "done" ? "text-green-400" : job.status === "failed" ? "text-red-400" : "text-indigo-400"}`}>
                    {job.message}
                  </p>
                  {job.status === "uploading" && job.progress > 0 && job.progress < 100 && (
                    <div className="mt-1.5 h-1 bg-white/10 rounded-full overflow-hidden">
                      <div
                        className="h-full bg-indigo-500 rounded-full transition-all duration-300"
                        style={{ width: `${job.progress}%` }}
                      />
                    </div>
                  )}
                </div>
                {(job.status === "done" || job.status === "failed") && (
                  <button
                    onClick={() => setJobs((prev) => prev.filter((j) => j.localId !== job.localId))}
                    className="shrink-0 text-gray-500 hover:text-white transition-colors"
                  >
                    <X className="w-4 h-4" />
                  </button>
                )}
              </div>
            ))}
          </div>
        )}

        {/* Video grid */}
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-3 gap-8">
          {videos.length === 0 && jobs.length === 0 && (
            <div className="text-gray-500 col-span-3">No uploaded videos yet.</div>
          )}
          {videos.map((video) => (
            <Card
              key={video.id}
              className="glass-panel overflow-hidden flex flex-col min-h-90 min-w-[320px] rounded-3xl border border-white/10 hover:border-indigo-500/30 shadow-xl hover:shadow-indigo-500/20 transform transition-all hover:-translate-y-2 duration-500 group bg-transparent"
            >
              <CardHeader className="pb-3 pt-5 relative z-10">
                <CardTitle
                  className="text-lg font-bold wrap-break-word whitespace-pre-line leading-tight truncate text-white drop-shadow-md"
                  title={video.title}
                >
                  {video.title || "Uploaded Video"}
                </CardTitle>
              </CardHeader>
              <CardContent className="flex-1 flex flex-col justify-between z-10 px-6">
                <div className="mb-5 text-xs flex gap-2">
                  <span className="inline-block px-3 py-1.5 bg-white/10 rounded-full text-indigo-300 font-medium tracking-wider uppercase text-[10px] border border-white/5">
                    {video.category}
                  </span>
                  {video.duration !== undefined && video.duration !== null && (
                    <span className="inline-block px-3 py-1.5 bg-black/30 rounded-full text-gray-300 font-mono text-[10px] border border-white/5">
                      {formatDuration(video.duration)}
                    </span>
                  )}
                  <span className="inline-block px-3 py-1.5 bg-purple-500/10 rounded-full text-purple-300 font-medium tracking-wider uppercase text-[10px] border border-purple-500/10">
                    720p
                  </span>
                </div>
                <div className="flex justify-between items-center rounded-xl overflow-hidden shadow-inner bg-black/40 relative group-hover:shadow-indigo-500/20 transition-all aspect-video">
                  <video
                    className="w-full h-full object-cover opacity-70 group-hover:opacity-100 transition-opacity duration-300"
                    controls
                    preload="metadata"
                  >
                    <source src={video.url} type="video/mp4" />
                    <source src={video.url} type="video/webm" />
                    Your browser does not support the video tag.
                  </video>
                </div>
              </CardContent>
              <CardFooter className="flex justify-end gap-2 pt-0 pb-5 pr-6 z-10">
                <Button
                  variant="outline"
                  size="icon"
                  title="Download Video"
                  onClick={async (e) => {
                    e.stopPropagation();
                    try {
                      const blob = await downloadVideo(token!, video.id);
                      const blobUrl = window.URL.createObjectURL(blob);
                      const a = document.createElement("a");
                      a.href = blobUrl;
                      const ext = video.url.split(".").pop()?.split("?")[0] || "mp4";
                      a.download = video.title
                        ? `${video.title.replace(/[^a-z0-9]/gi, "_").toLowerCase()}_720p.${ext}`
                        : `upload-${video.id}.${ext}`;
                      document.body.appendChild(a);
                      a.click();
                      document.body.removeChild(a);
                      window.URL.revokeObjectURL(blobUrl);
                    } catch {
                      window.open(video.url, "_blank");
                    }
                  }}
                  className="h-10 w-10 p-0 rounded-full bg-indigo-500/10 text-indigo-400 hover:bg-indigo-500 hover:text-white border border-indigo-500/20 transition-all shadow hover:shadow-indigo-500/40"
                >
                  <Download className="w-4 h-4" />
                </Button>
                <Button
                  variant="destructive"
                  size="icon"
                  onClick={(e) => {
                    e.stopPropagation();
                    setDeleteId(video.id);
                    setShowDialog(true);
                  }}
                  className="h-10 w-10 p-0 rounded-full bg-red-500/10 text-red-400 hover:bg-red-500 hover:text-white border border-red-500/20 transition-all shadow hover:shadow-red-500/40"
                >
                  <Trash className="w-4 h-4" />
                </Button>
              </CardFooter>
            </Card>
          ))}
        </div>
      </div>

      {/* Delete confirmation */}
      <Dialog open={showDialog} onOpenChange={setShowDialog}>
        <DialogContent className="glass-panel border-white/10 bg-gray-950/90 text-white rounded-4xl p-6 sm:p-8 shadow-2xl shadow-black">
          <DialogHeader>
            <DialogTitle className="text-xl">Delete Video</DialogTitle>
          </DialogHeader>
          <div className="text-gray-300 my-2">
            Are you sure you want to delete this video? The file will be permanently removed.
          </div>
          <DialogFooter className="mt-4 gap-2">
            <Button
              variant="outline"
              onClick={() => setShowDialog(false)}
              className="rounded-xl border-white/10 hover:bg-white/10 hover:text-white text-gray-300"
            >
              Cancel
            </Button>
            <Button
              onClick={confirmDelete}
              className="bg-red-500/20 text-red-400 hover:bg-red-500/40 hover:text-white border border-red-500/30 transition-all rounded-xl shadow-lg shadow-red-500/20"
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  );
}
```

- [ ] **Step 2: Verify TypeScript compiles**

```bash
cd /Users/adibshakib/Coding/vidQ/frontend
npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/app/upload/page.tsx
git commit -m "feat: add /upload page with drag-and-drop and 720p video grid"
```

---

## Task 7: Add "Upload Video" nav link to home page header

**Files:**
- Modify: `frontend/app/page.tsx` (lines ~355–371)

- [ ] **Step 1: Add the nav link to the header in `page.tsx`**

In `frontend/app/page.tsx`, replace the header `<div>` (the one containing the VidQ title and Logout button) with:

```tsx
<div className="flex justify-between items-center px-8 py-5 glass-panel sticky top-0 z-50 rounded-b-2xl mx-4 mb-10 shadow-xl shadow-indigo-500/10">
  <h1
    className="text-2xl font-bold bg-clip-text text-transparent bg-linear-to-r from-indigo-400 to-purple-400 cursor-pointer"
    onClick={() => window.location.reload()}
  >
    VidQ
  </h1>
  <div className="flex items-center gap-3">
    <Link href="/upload">
      <Button
        variant="outline"
        className="border-white/10 bg-transparent hover:bg-white/10 hover:text-white transition-all rounded-xl text-gray-200 text-sm"
      >
        Upload Video
      </Button>
    </Link>
    {authEnabled && (
      <Button
        variant="outline"
        onClick={logout}
        className="border-white/10 bg-transparent hover:bg-white/10 hover:text-white transition-all rounded-xl text-gray-200"
      >
        Logout
      </Button>
    )}
  </div>
</div>
```

Also add `import Link from "next/link";` at the top of `page.tsx` with the other imports.

- [ ] **Step 2: Verify TypeScript compiles**

```bash
cd /Users/adibshakib/Coding/vidQ/frontend
npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat: add Upload Video nav link to home page header"
```

---

## Final Verification

- [ ] **Run full backend test suite**

```bash
cd /Users/adibshakib/Coding/vidQ/backend
.venv/bin/pytest -v
```

Expected: all tests pass.

- [ ] **Start both servers and test manually**

```bash
# Terminal 1
cd /Users/adibshakib/Coding/vidQ/backend
.venv/bin/uvicorn app.main:app --reload

# Terminal 2
cd /Users/adibshakib/Coding/vidQ/frontend
npm run dev
```

Open `http://localhost:3000`. Click "Upload Video" in the header. Enter a category, drag a video file into the zone, confirm progress card appears, and verify the scaled video appears in the grid below with working download and delete.
