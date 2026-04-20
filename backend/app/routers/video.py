from fastapi import APIRouter, Depends, HTTPException, Body
from sqlalchemy.orm import Session
from app.db import get_db, Video
from app.models import VideoCreate, VideoOut
from app.routers.auth import verify_token
from typing import List, Optional
import requests
from bs4 import BeautifulSoup
from app.services.llm_manager import FallbackLLMManager
from app.services.scraper import run_extraction, USER_AGENTS, clean_html
from app.services.prompts import Prompts
import random
from app.config import get_settings, Settings
from urllib.parse import urlparse
import ipaddress
import logging

logger = logging.getLogger("VideoRouter")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('[%(levelname)s] [Router] %(message)s'))
    logger.addHandler(handler)

router = APIRouter()

# Lazy import singletons to avoid circular imports at module load time
def _get_llm_manager() -> FallbackLLMManager:
    from app.state import llm_manager
    return llm_manager

def _get_queue():
    from app.services.queue import video_queue
    return video_queue

def is_safe_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        if parsed.scheme not in ["http", "https"]:
            return False
        hostname = parsed.hostname
        if not hostname:
            return False
            
        # Explicitly allow temp_storage loopbacks so saving blob files doesn't throw 400 Bad Request
        if hostname.lower() in ["localhost", "127.0.0.1", "0.0.0.0"]:
            if parsed.path.startswith("/temp_storage/"):
                return True
            return False
            
        # Prevent internal IP blocks
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback:
                return False
        except ValueError:
            pass # Not an IP, assume domain is external

        return True
    except Exception:
        return False

def extract_title_and_duration(url: str) -> tuple[str, Optional[float]]:
    try:
        resp = requests.get(url, timeout=8, headers={"User-Agent": random.choice(USER_AGENTS)})
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        # Title
        title = None
        og = soup.find("meta", property="og:title", content=True)
        if og:
            title = og["content"]
        else:
            t = soup.find("title")
            title = t.text.strip() if t else url
        # Duration
        duration = None
        # Try <video duration>
        video_tag = soup.find("video")
        if video_tag and video_tag.has_attr("duration"):
            try:
                duration = float(video_tag["duration"])
            except Exception:
                pass
        # Try og:video:duration
        ogd = soup.find("meta", property="og:video:duration", content=True)
        if ogd:
            try:
                duration = float(ogd["content"])
            except Exception:
                pass
        # Try <meta name="duration">
        meta_dur = soup.find("meta", attrs={"name": "duration"}, content=True)
        if meta_dur:
            try:
                duration = float(meta_dur["content"])
            except Exception:
                pass
        return title, duration
    except Exception:
        return url, None

@router.post("/videos", response_model=VideoOut)
def add_video(video: VideoCreate, db: Session = Depends(get_db), token: str = Depends(verify_token)):
    if not is_safe_url(video.url):
        raise HTTPException(status_code=400, detail="Forbidden URL strictly isolated.")
        
    # Prevent duplicate videos by URL (normalize URL)
    norm_url = video.url.strip().lower()
    existing = db.query(Video).filter(Video.url == norm_url).first()
    
    # Also actively deduplicate by exact title (highly useful for temp Blob streams)
    if not existing and video.title and video.title.strip() not in ["", "Untitled Video", "Video"]:
        existing = db.query(Video).filter(Video.title == video.title.strip()).first()
        
    if existing:
        raise HTTPException(status_code=409, detail="Video already exists")
        
    # Fetch title and duration if not provided
    title = video.title
    duration = video.duration
    if not title or duration is None:
        scraped_title, scraped_duration = extract_title_and_duration(video.url)
        if not title:
            title = scraped_title
        if duration is None:
            duration = scraped_duration
    db_video = Video(url=norm_url, category=video.category, title=title, duration=duration)
    db.add(db_video)
    db.commit()
    db.refresh(db_video)
    return db_video

@router.get("/videos", response_model=List[VideoOut])
def list_videos(category: Optional[str] = None, db: Session = Depends(get_db), token: str = Depends(verify_token)):
    query = db.query(Video)
    if category:
        query = query.filter(Video.category == category)
    return query.order_by(Video.created_at.desc()).all()

@router.get("/videos/categories", response_model=List[str])
def list_categories(db: Session = Depends(get_db), token: str = Depends(verify_token)):
    categories = db.query(Video.category).distinct().all()
    return [c[0] for c in categories]

@router.delete("/videos/{video_id}", status_code=204)
def delete_video(video_id: int, db: Session = Depends(get_db), token: str = Depends(verify_token)):
    video = db.query(Video).filter(Video.id == video_id).first()
    if not video:
        raise HTTPException(status_code=404, detail="Video not found")
    db.delete(video)
    db.commit()
    return None

async def call_llm_with_html_and_screenshot(llm_manager: FallbackLLMManager, html: str, screenshot_b64: str, network_video_urls: list[str], thumbnail_url: str) -> dict:
    prompt = Prompts.video_metadata(clean_html(html), network_video_urls)
    try:
        logger.debug(f"Passing payload to LLM (vision): HTML={len(html)}chars, URLs={len(network_video_urls)}")
        result = await llm_manager.execute(prompt, screenshot_b64)
        logger.debug(f"LLM Metadata Result: {result}")
        return result
    except Exception as e:
        logger.error(f"LLM Manager execution failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"LLM extraction failed: {e}")

@router.post("/extract-video")
async def extract_video_llm(data: dict = Body(...), token: str = Depends(verify_token)):
    url = data.get("url")
    logger.info(f"Received video extraction request for URL: {url}")
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")
    if not is_safe_url(url):
        raise HTTPException(status_code=400, detail="Forbidden URL strictly isolated.")
    
    llm_manager = _get_llm_manager()
    user_agent = random.choice(USER_AGENTS)
    
    try:
        html, screenshot_b64, network_video_urls, thumbnail_url, temp_video_url = await run_extraction(
            url=url,
            user_agent=user_agent,
            llm_manager=llm_manager
        )
    except Exception as e:
        logger.error(f"Playwright scraping failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e))
        
    result = await call_llm_with_html_and_screenshot(llm_manager, html, screenshot_b64, network_video_urls, thumbnail_url)
    result["thumbnail"] = result.get("thumbnail") or thumbnail_url
    
    if not result.get("video_url") and temp_video_url:
        result["video_url"] = temp_video_url
        
    return result


@router.post("/queue", status_code=200)
def enqueue_video(data: dict = Body(...), token: str = Depends(verify_token)):
    """Enqueue a long-running video recording job (up to 2h). Returns immediately."""
    url = data.get("url")
    category = data.get("category", "uncategorized")
    if not url:
        raise HTTPException(status_code=400, detail="Missing url")
    if not is_safe_url(url):
        raise HTTPException(status_code=400, detail="Forbidden URL strictly isolated.")

    queue = _get_queue()
    job = queue.enqueue(url=url, category=category, token=token)
    position = queue.position(job.job_id)

    return {
        "message": "Video queued for processing. It will be available once recording is complete.",
        "job_id": job.job_id,
        "queue_position": position,
        "status": job.status,
    }


@router.get("/queue/{job_id}")
def get_queue_status(job_id: str, token: str = Depends(verify_token)):
    """Poll job status. Returns status, and result/error when done."""
    queue = _get_queue()
    job = queue.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")

    response = {
        "job_id": job.job_id,
        "url": job.url,
        "status": job.status,
    }

    if job.status == "queued":
        response["queue_position"] = queue.position(job_id)
    elif job.status == "done":
        response["result"] = job.result
    elif job.status == "failed":
        response["error"] = job.error

    return response
