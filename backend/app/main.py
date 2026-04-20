from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.routers.auth import router as auth_router
from app.routers.video import router as video_router
from app.config import get_settings

app = FastAPI()

import os
settings = get_settings()
os.makedirs(settings.temp_storage_dir, exist_ok=True)

app.mount("/temp_storage", StaticFiles(directory=settings.temp_storage_dir), name="temp_storage")

if not settings.cors_origins:
    raise ValueError("Missing required environment variable: CORS_ORIGINS")

# Allow local frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins.split(","),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(video_router)
