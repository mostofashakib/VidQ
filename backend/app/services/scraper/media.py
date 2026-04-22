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
            final_path = _ensure_min_quality(out_path)
            final_filename = os.path.basename(final_path)
            size_kb = os.path.getsize(final_path) // 1024
            logger.info(f"ffmpeg download succeeded: {size_kb}KB → {final_filename}")
            return f"{_settings.base_url}/temp_storage/{final_filename}"
        err_tail = (stderr or b'').decode(errors='replace')[-300:]
        logger.warning(f"ffmpeg download failed (rc={proc.returncode}): {err_tail}")
    except Exception as e:
        logger.warning(f"ffmpeg download error: {e}")
    return None


def _probe_video_dimensions(video_path: str) -> tuple[int, int] | None:
    """Return (width, height) by running ffmpeg -i; None if unable to probe."""
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        result = subprocess.run(
            [ffmpeg_exe, "-i", video_path],
            capture_output=True, text=True,
        )
        # ffmpeg prints stream info to stderr (exit code 1 is expected — no output file)
        match = re.search(r'Video:.*?(\d{2,5})x(\d{2,5})', result.stderr)
        if match:
            return int(match.group(1)), int(match.group(2))
    except Exception:
        pass
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


def _ensure_min_quality(video_path: str, min_height: int = 720) -> str:
    """
    Resize video to exactly min_height: upscale if below, downscale if above.
    Uses Lanczos scaling with CRF 18 to preserve visual quality in both directions.
    Returns the final file path (may differ from input when rescaling occurs).
    """
    dims = _probe_video_dimensions(video_path)
    if dims is None:
        logger.warning(f"Could not probe {os.path.basename(video_path)} — skipping quality check")
        return video_path

    width, height = dims
    if height == min_height:
        logger.info(f"Video {width}x{height} — already at {min_height}p")
        return video_path

    direction = "upscaling" if height < min_height else "downscaling"
    logger.info(f"Video is {width}x{height} — {direction} to {min_height}p with Lanczos")
    base, ext = os.path.splitext(video_path)
    out_path = f"{base}_{min_height}p{ext}"
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        cmd = [
            ffmpeg_exe, "-y",
            "-i", video_path,
            "-vf", f"scale=-2:{min_height}:flags=lanczos",
            "-c:v", "libx264",
            "-crf", "18",
            "-preset", "slow",
            "-c:a", "copy",
            out_path,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if result.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10_000:
            os.remove(video_path)
            size_kb = os.path.getsize(out_path) // 1024
            logger.info(f"Rescaled to {min_height}p: {os.path.basename(out_path)} ({size_kb}KB)")
            return out_path
        logger.error(f"Rescale failed (rc={result.returncode}): {result.stderr[-300:]}")
    except subprocess.TimeoutExpired:
        logger.error(f"Rescale timed out for {os.path.basename(video_path)}")
    except Exception as e:
        logger.error(f"Rescale error: {e}")
    return video_path


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
        final_path = _ensure_min_quality(mp4_path)
        logger.info(f"Converted to {os.path.basename(final_path)}")
        return final_path
    except Exception as e:
        logger.error(f"MP4 conversion failed: {e}")
        return webm_path
