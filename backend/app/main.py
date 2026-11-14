from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from app.logging_config import configure_logging
from app.routers.auth import router as auth_router
from app.routers.video import router as video_router
from app.routers.upload import router as upload_router
from app.routers.combine import router as combine_router
from app.routers.translate import router as translate_router
from app.routers.trim import router as trim_router
from app.routers.enhance import router as enhance_router
from app.routers.search import router as search_router
from app.config import get_settings

configure_logging()

app = FastAPI()

import os
settings = get_settings()
os.makedirs(settings.temp_storage_dir, exist_ok=True)

app.mount("/temp_storage", StaticFiles(directory=settings.temp_storage_dir), name="temp_storage")

if not settings.cors_origins:
    raise ValueError("Missing required environment variable: CORS_ORIGINS")

allowed_origins = {origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()}
# Ensure both localhost and 127.0.0.1 variants are allowed for local development
allowed_origins.update(["http://localhost:3000", "http://127.0.0.1:3000"])

# Allow local frontend
app.add_middleware(
    CORSMiddleware,
    allow_origins=list(allowed_origins),
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:[0-9]+)?$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router)
app.include_router(video_router)
app.include_router(upload_router)
app.include_router(combine_router)
app.include_router(translate_router)
app.include_router(trim_router)
app.include_router(enhance_router)
app.include_router(search_router)
