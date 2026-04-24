import asyncio
import logging
import os
import re
import subprocess
import uuid
from urllib.parse import urlparse, urljoin

import httpx
import imageio_ffmpeg
from bs4 import BeautifulSoup

from app.config import get_settings
from app.services.video_utils import ensure_min_quality

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


_DIRECT_VIDEO_EXTENSIONS = frozenset([".mp4", ".m3u8", ".webm", ".mov", ".flv", ".avi", ".mkv"])
_DIRECT_VIDEO_CONTENT_TYPES = frozenset(["video/mp4", "video/webm", "video/ogg", "application/x-mpegurl",
                                          "application/vnd.apple.mpegurl", "video/x-flv", "video/quicktime"])


async def _detect_direct_video_embed(url: str, user_agent: str) -> tuple[str, str] | None:
    """
    Determine if the URL is a directly downloadable video (returns (url, ""))
    or a minimal embed page wrapping a video (returns (video_src, html)).
    Returns None for full web pages that need Playwright.
    """
    headers = {"User-Agent": user_agent}

    # Fast path: URL path ends with a known video extension — no HTTP needed
    path = urlparse(url).path.lower()
    if any(path.endswith(ext) for ext in _DIRECT_VIDEO_EXTENSIONS):
        logger.info(f"Direct video URL detected by extension: {url[:100]}")
        return url, ""

    # Cheap HEAD request to check Content-Type before downloading the body
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            head = await client.head(url, headers=headers)
            content_type = head.headers.get("content-type", "").split(";")[0].strip().lower()
            if content_type in _DIRECT_VIDEO_CONTENT_TYPES:
                logger.info(f"Direct video URL detected by Content-Type ({content_type}): {url[:100]}")
                return url, ""
            if head.status_code != 200 or "text/html" not in content_type:
                return None
    except Exception:
        return None

    # Full GET only for HTML pages — check if it's a minimal video embed
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                return None
            html = resp.text
    except Exception:
        return None

    soup = BeautifulSoup(html, "html.parser")
    body = soup.body
    if not body:
        return None

    videos = body.find_all("video")
    if not videos:
        return None

    # Extract the first usable direct HTTP video URL
    video_url: str | None = None
    for video in videos:
        src = video.get("src") or ""
        if src and not src.startswith("blob:"):
            if src.startswith("/"):
                src = urljoin(url, src)
            if src.startswith("http"):
                video_url = src
                break
        source = video.find("source")
        if source:
            src = source.get("src") or ""
            if src:
                if src.startswith("/"):
                    src = urljoin(url, src)
                if src.startswith("http") and not src.startswith("blob:"):
                    video_url = src
                    break

    if not video_url:
        return None

    # Reject pages with substantial surrounding content
    for tag in body.find_all(["script", "style", "noscript"]):
        tag.decompose()
    block_tags = body.find_all(["p", "article", "section", "nav", "header", "footer", "aside",
                                 "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table"])
    if len(block_tags) > 3:
        return None
    if len(body.get_text(separator=" ", strip=True)) > 500:
        return None

    logger.info(f"Embedded video page detected, src: {video_url[:100]}")
    return video_url, html


async def _download_embed_video(
    video_url: str,
    referer: str,
    timeout_s: int = 300,
) -> str | None:
    """
    Download a direct embed video using curl (mp4/webm) or yt-dlp (m3u8/HLS).
    Resizes to 720p if needed. Returns a localhost URL or None on failure.
    """
    storage = _settings.temp_storage_dir
    os.makedirs(storage, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.mp4"
    out_path = os.path.join(storage, filename)

    is_m3u8 = ".m3u8" in video_url.lower()

    if is_m3u8:
        cmd = [
            "yt-dlp",
            "--merge-output-format", "mp4",
            "-o", out_path,
            video_url,
        ]
        label = "yt-dlp"
    else:
        cmd = [
            "curl", "-L", "-s",
            "--referer", referer,
            "-o", out_path,
            video_url,
        ]
        label = "curl"

    logger.info(f"{label} embed download: {video_url[:100]}")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
    except asyncio.TimeoutError:
        proc.kill()
        logger.warning(f"{label} embed download timed out ({timeout_s}s)")
        return None

    if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10_000:
        final_path = ensure_min_quality(out_path)
        size_kb = os.path.getsize(final_path) // 1024
        logger.info(f"{label} embed download succeeded: {size_kb}KB → {os.path.basename(final_path)}")
        return f"{_settings.base_url}/temp_storage/{os.path.basename(final_path)}"

    err_tail = (stderr or b"").decode(errors="replace")[-300:]
    logger.warning(f"{label} embed download failed (rc={proc.returncode}): {err_tail}")
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
            final_path = ensure_min_quality(out_path)
            final_filename = os.path.basename(final_path)
            size_kb = os.path.getsize(final_path) // 1024
            logger.info(f"ffmpeg download succeeded: {size_kb}KB → {final_filename}")
            return f"{_settings.base_url}/temp_storage/{final_filename}"
        err_tail = (stderr or b'').decode(errors='replace')[-300:]
        logger.warning(f"ffmpeg download failed (rc={proc.returncode}): {err_tail}")
    except Exception as e:
        logger.warning(f"ffmpeg download error: {e}")
    return None


def _probe_file_duration(video_path: str) -> float | None:
    """Return video duration in seconds, or None if unable to probe."""
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        result = subprocess.run(
            [ffmpeg_exe, "-i", video_path],
            capture_output=True, text=True,
        )
        match = re.search(r'Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)', result.stderr)
        if match:
            h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
            return h * 3600 + m * 60 + s
    except Exception:
        pass
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
        final_path = ensure_min_quality(mp4_path)
        logger.info(f"Converted to {os.path.basename(final_path)}")
        return final_path
    except Exception as e:
        logger.error(f"MP4 conversion failed: {e}")
        return webm_path
