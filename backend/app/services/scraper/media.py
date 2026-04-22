import asyncio
import logging
import os
import re
import subprocess
import uuid
from urllib.parse import urlparse

import httpx
import imageio_ffmpeg

from app.config import get_settings

logger = logging.getLogger("VideoScraper")

_settings = get_settings()

_AD_DOMAINS = frozenset([
    'tsyndicate.com', 'svacdn.tsyndicate.com',
    'doubleclick.net', 'googlesyndication.com',
    'adnxs.com', 'advertising.com', 'adsrvr.org',
    'adcolony.com', 'inmobi.com', 'rubiconproject.com',
    'pubmatic.com', 'openx.net', 'triplelift.com',
    'moatads.com', 'scorecardresearch.com',
])
# Matches ad-size dimension patterns in URL paths: 440x250, 320x240, 160x90, etc.
_AD_SIZE_RE = re.compile(r'\b\d{2,3}x\d{2,3}\b')


def _is_ad_video_url(url: str) -> bool:
    """Return True if url looks like an ad, promo, or related-video thumbnail URL."""
    try:
        parsed = urlparse(url)
        host = (parsed.hostname or '').lower()
        if any(host == d or host.endswith('.' + d) for d in _AD_DOMAINS):
            return True
        if _AD_SIZE_RE.search(parsed.path):
            return True
    except Exception:
        pass
    return False


async def _is_forbidden(url: str, user_agent: str) -> bool:
    if not url or url.startswith("blob:"):
        return True
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.head(url, headers={"User-Agent": user_agent}, follow_redirects=True)
            return resp.status_code in (401, 403, 404)
    except Exception:
        return False


async def _get_main_playing_video_url(page) -> str | None:
    """
    Return the HTTP URL currently loaded by the largest (main) video element,
    checking the main frame and all child iframes.
    Prefers currentSrc (what's actually playing) over the src attribute.
    """
    _js = '''() => {
        const videos = Array.from(document.querySelectorAll('video'));
        if (!videos.length) return null;
        const main = videos.reduce((best, v) => {
            const area = (v.offsetWidth || 0) * (v.offsetHeight || 0);
            const bestArea = (best.offsetWidth || 0) * (best.offsetHeight || 0);
            return area > bestArea ? v : best;
        }, videos[0]);
        const u = main.currentSrc || main.getAttribute('src') || '';
        return (u && !u.startsWith('blob:') && u.startsWith('http')) ? u : null;
    }'''
    for target in [page, *page.frames[1:]]:
        try:
            result = await target.evaluate(_js)
            if result:
                logger.info(f"Main video DOM currentSrc: {result[:100]}")
                return result
        except Exception:
            pass
    return None


async def _download_video_direct(
    video_url: str,
    referer: str,
    user_agent: str,
    timeout_s: int = 120,
) -> str | None:
    """
    Download video_url via ffmpeg stream-copy with Referer + User-Agent headers.
    Returns a localhost URL to the saved file, or None on failure.
    """
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        storage = _settings.temp_storage_dir
        os.makedirs(storage, exist_ok=True)
        filename = f"{uuid.uuid4().hex}.mp4"
        out_path = os.path.join(storage, filename)
        cmd = [
            ffmpeg_exe, "-y",
            "-user_agent", user_agent,
            "-headers", f"Referer: {referer}\r\n",
            "-i", video_url,
            "-c", "copy",
            out_path,
        ]
        logger.info(f"ffmpeg direct download: {video_url[:100]}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(f"ffmpeg download timed out ({timeout_s}s)")
            return None
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10_000:
            size_kb = os.path.getsize(out_path) // 1024
            logger.info(f"ffmpeg download succeeded: {size_kb}KB → {filename}")
            return f"{_settings.base_url}/temp_storage/{filename}"
        err_tail = (stderr or b'').decode(errors='replace')[-300:]
        logger.warning(f"ffmpeg download failed (rc={proc.returncode}): {err_tail}")
    except Exception as e:
        logger.warning(f"ffmpeg download error: {e}")
    return None


def _convert_to_mp4(webm_path: str) -> str:
    """Convert webm to mp4 via ffmpeg, return final path."""
    if not os.path.exists(webm_path):
        return webm_path
    mp4_path = webm_path.replace(".webm", ".mp4")
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        logger.info(f"Converting {os.path.basename(webm_path)} to MP4…")
        cmd = [
            ffmpeg_exe, "-y",
            "-i", webm_path,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264",
            "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            mp4_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"FFMPEG error: {result.stderr}")
            return webm_path
        os.remove(webm_path)
        logger.info(f"Converted to {os.path.basename(mp4_path)}")
        return mp4_path
    except Exception as e:
        logger.error(f"MP4 conversion failed: {e}")
        return webm_path
