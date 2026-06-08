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


async def _try_ytdlp_on_page(
    url: str,
    user_agent: str,
    referer: str | None = None,
    timeout_s: int = 90,
) -> str | None:
    """
    Try yt-dlp directly on a web page URL.

    yt-dlp has extractors for hundreds of video hosting platforms and a
    generic extractor that understands jwplayer, video.js, and similar
    embedded players.  It also handles token refresh for HLS streams and
    cookie-based auth — things that raw ffmpeg can't do.

    Returns a localhost temp URL for the downloaded file, or None when
    yt-dlp doesn't support the site or the download fails.
    """
    storage = _settings.temp_storage_dir
    os.makedirs(storage, exist_ok=True)
    filename = f"{uuid.uuid4().hex}.mp4"
    out_path = os.path.join(storage, filename)

    cmd = [
        "yt-dlp",
        "--no-playlist",
        "--merge-output-format", "mp4",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--user-agent", user_agent,
        "--add-header", f"Referer:{referer or url}",
        "-o", out_path,
        url,
    ]
    logger.info(f"yt-dlp page extraction: {url[:100]}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(f"yt-dlp page extraction timed out ({timeout_s}s)")
            return None

        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10_000:
            final_path = ensure_min_quality(out_path)
            if not _validate_mp4(final_path):
                logger.warning("yt-dlp page extraction: file failed MP4 validation — discarding")
                try:
                    os.remove(final_path)
                except Exception:
                    pass
                return None
            size_kb = os.path.getsize(final_path) // 1024
            logger.info(f"yt-dlp page extraction succeeded: {size_kb}KB → {os.path.basename(final_path)}")
            return f"{_settings.base_url}/temp_storage/{os.path.basename(final_path)}"

        stderr_text = (stderr or b"").decode(errors="replace")
        if "Unsupported URL" in stderr_text or "Unable to extract" in stderr_text:
            logger.debug(f"yt-dlp: site not supported or no video found ({url[:60]})")
        else:
            logger.warning(f"yt-dlp page extraction failed (rc={proc.returncode}): {stderr_text[-300:]}")
    except Exception as e:
        logger.warning(f"yt-dlp page extraction error: {e}")
    return None


async def _download_embed_video(
    video_url: str,
    referer: str,
    user_agent: str = "",
    timeout_s: int = 300,
) -> str | None:
    """
    Download a direct embed video using ffmpeg (mp4/webm/m3u8) with curl as
    fallback for plain files.  Resizes to 720p if needed.
    Returns a localhost URL or None on failure.
    """
    storage = _settings.temp_storage_dir
    os.makedirs(storage, exist_ok=True)

    v_lower = video_url.lower()
    is_m3u8 = ".m3u8" in v_lower or "m3u8" in urlparse(video_url).path.lower()

    ffmpeg_exe = None
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        pass

    # ── Primary: ffmpeg stream-copy (fast, handles m3u8 + direct video) ──────
    if ffmpeg_exe:
        filename = f"{uuid.uuid4().hex}.mp4"
        out_path = os.path.join(storage, filename)
        ffmpeg_headers = f"Referer: {referer}\r\n"
        if user_agent:
            ffmpeg_headers += f"User-Agent: {user_agent}\r\n"
        cmd = [
            ffmpeg_exe, "-y",
            "-headers", ffmpeg_headers,
            "-i", video_url,
            "-c", "copy",
            out_path,
        ]
        logger.info(f"ffmpeg embed download: {video_url[:100]}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(f"ffmpeg embed download timed out ({timeout_s}s)")
            stderr_bytes = b""
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10_000:
            final_path = ensure_min_quality(out_path)
            if _validate_mp4(final_path):
                size_kb = os.path.getsize(final_path) // 1024
                logger.info(f"ffmpeg embed download succeeded: {size_kb}KB → {os.path.basename(final_path)}")
                return f"{_settings.base_url}/temp_storage/{os.path.basename(final_path)}"
            try:
                os.remove(final_path)
            except Exception:
                pass
        err_tail = (stderr_bytes or b"").decode(errors="replace")[-200:]
        logger.warning(f"ffmpeg embed download failed (rc={proc.returncode}): {err_tail}")

    # ── Fallback: yt-dlp (handles auth-gated HLS, encrypted segments, etc.) ──
    filename = f"{uuid.uuid4().hex}.mp4"
    out_path = os.path.join(storage, filename)
    ytdlp_cmd = [
        "yt-dlp",
        "--no-playlist",
        "--merge-output-format", "mp4",
        "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "--add-header", f"Referer:{referer}",
        "-o", out_path,
    ]
    if user_agent:
        ytdlp_cmd += ["--user-agent", user_agent]
    ytdlp_cmd.append(video_url)

    logger.info(f"yt-dlp embed fallback: {video_url[:100]}")
    try:
        proc = await asyncio.create_subprocess_exec(
            *ytdlp_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr_bytes = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(f"yt-dlp embed fallback timed out ({timeout_s}s)")
            return None
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10_000:
            final_path = ensure_min_quality(out_path)
            if not _validate_mp4(final_path):
                logger.warning("yt-dlp embed fallback: file failed validation — discarding")
                try:
                    os.remove(final_path)
                except Exception:
                    pass
                return None
            size_kb = os.path.getsize(final_path) // 1024
            logger.info(f"yt-dlp embed fallback succeeded: {size_kb}KB → {os.path.basename(final_path)}")
            return f"{_settings.base_url}/temp_storage/{os.path.basename(final_path)}"
        err_tail = (stderr_bytes or b"").decode(errors="replace")[-300:]
        logger.warning(f"yt-dlp embed fallback failed (rc={proc.returncode}): {err_tail}")
    except Exception as e:
        logger.warning(f"yt-dlp embed fallback error: {e}")
    return None


async def _download_video_direct(
    video_url: str,
    referer: str,
    user_agent: str,
    timeout_s: int = 120,
    total_duration_s: float | None = None,
    progress_callback=None,
) -> str | None:
    """
    Download video_url. Tries ffmpeg first (fast stream-copy); falls back to
    yt-dlp for m3u8/DASH URLs where ffmpeg fails due to token refresh or
    segment encryption.
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
            "-progress", "pipe:1",
            "-nostats",
            out_path,
        ]
        logger.info(f"ffmpeg direct download: {video_url[:100]}")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        stderr_chunks: list[bytes] = []

        async def _drain_stderr() -> None:
            try:
                while True:
                    chunk = await proc.stderr.read(4096)
                    if not chunk:
                        break
                    stderr_chunks.append(chunk)
            except Exception:
                pass

        stderr_task = asyncio.create_task(_drain_stderr())

        # Stream stdout for real-time progress (ffmpeg -progress pipe:1 output)
        deadline = asyncio.get_event_loop().time() + timeout_s
        try:
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    proc.kill()
                    logger.warning(f"ffmpeg download timed out ({timeout_s}s)")
                    break
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=min(remaining, 10))
                except asyncio.TimeoutError:
                    if asyncio.get_event_loop().time() >= deadline:
                        proc.kill()
                        logger.warning(f"ffmpeg download timed out ({timeout_s}s)")
                    break
                if not line:
                    break
                line_str = line.decode(errors="replace").strip()
                if line_str.startswith("out_time=") and total_duration_s and total_duration_s > 0:
                    time_str = line_str[len("out_time="):]
                    try:
                        parts = time_str.split(":")
                        current_s = int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
                        pct = min(99, int(current_s / total_duration_s * 100))
                        if progress_callback:
                            progress_callback(pct)
                    except Exception:
                        pass
        except Exception:
            pass

        await proc.wait()
        stderr_task.cancel()
        try:
            await asyncio.wait_for(stderr_task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
        stderr = b"".join(stderr_chunks)

        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10_000:
            final_path = ensure_min_quality(out_path)
            if not _validate_mp4(final_path):
                logger.warning("ffmpeg download: file failed MP4 validation — discarding")
                try:
                    os.remove(final_path)
                except Exception:
                    pass
            else:
                final_filename = os.path.basename(final_path)
                size_kb = os.path.getsize(final_path) // 1024
                logger.info(f"ffmpeg download succeeded: {size_kb}KB → {final_filename}")
                return f"{_settings.base_url}/temp_storage/{final_filename}"
        err_tail = (stderr or b'').decode(errors='replace')[-300:]
        logger.warning(f"ffmpeg download failed (rc={proc.returncode}): {err_tail}")
    except Exception as e:
        logger.warning(f"ffmpeg download error: {e}")

    # yt-dlp fallback — handles tokenized HLS/DASH and CDN URLs that ffmpeg
    # can't authenticate.  Always triggered after ffmpeg failure so that auth-
    # gated direct MP4s and encrypted HLS segments both get a second chance.
    v_lower = video_url.lower()
    r_path = urlparse(video_url).path.lower()
    is_manifest = r_path.endswith(".m3u8") or r_path.endswith(".mpd") or "m3u8" in v_lower
    logger.info(f"yt-dlp {'manifest' if is_manifest else 'direct'} fallback: {video_url[:100]}")
    try:
        storage = _settings.temp_storage_dir
        filename = f"{uuid.uuid4().hex}.mp4"
        out_path = os.path.join(storage, filename)
        cmd = [
            "yt-dlp",
            "--no-playlist",
            "--merge-output-format", "mp4",
            "-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
            "--user-agent", user_agent,
            "--add-header", f"Referer:{referer}",
            "-o", out_path,
            video_url,
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        except asyncio.TimeoutError:
            proc.kill()
            logger.warning(f"yt-dlp download timed out ({timeout_s}s)")
            return None
        if proc.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 10_000:
            final_path = ensure_min_quality(out_path)
            if not _validate_mp4(final_path):
                logger.warning("yt-dlp download: file failed MP4 validation — discarding")
                try:
                    os.remove(final_path)
                except Exception:
                    pass
            else:
                final_filename = os.path.basename(final_path)
                size_kb = os.path.getsize(final_path) // 1024
                logger.info(f"yt-dlp download succeeded: {size_kb}KB → {final_filename}")
                return f"{_settings.base_url}/temp_storage/{final_filename}"
        err_tail = (stderr or b'').decode(errors='replace')[-300:]
        logger.warning(f"yt-dlp download failed (rc={proc.returncode}): {err_tail}")
    except Exception as e:
        logger.warning(f"yt-dlp download error: {e}")

    return None


def _validate_video_file(path: str, label: str = "video") -> bool:
    """
    Return True if `path` is a valid media file with a real video stream.

    Uses `ffmpeg -i` stderr parsing — the same pattern used elsewhere in this
    file — to verify three conditions:
      1. File exists and is at least 50 KB (rules out empty/HTML error saves).
      2. A Video: stream line is present with non-zero dimensions.
      3. Duration is at least 1 second.
    """
    if not os.path.exists(path):
        logger.warning(f"Validation: {label} file not found: {path}")
        return False
    size = os.path.getsize(path)
    if size < 50_000:
        logger.warning(
            f"Validation: {label} {os.path.basename(path)} is only {size} bytes — "
            "likely corrupt or empty"
        )
        return False
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        result = subprocess.run(
            [ffmpeg_exe, "-i", path],
            capture_output=True, text=True, timeout=15,
        )
        stderr = result.stderr
        if not re.search(r"Video:.*\d{2,5}x\d{2,5}", stderr):
            logger.warning(
                f"Validation: no video stream found in {label} {os.path.basename(path)}"
            )
            return False
        dur_match = re.search(r"Duration:\s+(\d+):(\d+):(\d+(?:\.\d+)?)", stderr)
        if not dur_match:
            logger.warning(
                f"Validation: could not parse duration in {label} {os.path.basename(path)}"
            )
            return False
        h, m, s = int(dur_match.group(1)), int(dur_match.group(2)), float(dur_match.group(3))
        duration = h * 3600 + m * 60 + s
        if duration < 1.0:
            logger.warning(
                f"Validation: {label} {os.path.basename(path)} duration too short "
                f"({duration:.2f}s)"
            )
            return False
        logger.debug(
            f"Validation OK: {label} {os.path.basename(path)} "
            f"({duration:.1f}s, has video stream)"
        )
        return True
    except Exception as e:
        logger.warning(f"Validation error for {label} {os.path.basename(path)}: {e}")
        return False


def _validate_mp4(path: str) -> bool:
    return _validate_video_file(path, label="MP4")


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


def _convert_to_mp4(webm_path: str) -> str | None:
    """Convert WebM to MP4 via ffmpeg. Return final path, or None if invalid."""
    if not os.path.exists(webm_path):
        logger.warning(f"WebM conversion skipped: file not found: {webm_path}")
        return None
    if not _validate_video_file(webm_path, label="WebM recording"):
        logger.error(
            f"MediaRecorder WebM is invalid before conversion: "
            f"{os.path.basename(webm_path)}"
        )
        try:
            os.remove(webm_path)
        except Exception:
            pass
        return None
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
            return None
        os.remove(webm_path)
        final_path = ensure_min_quality(mp4_path)
        if not _validate_mp4(final_path):
            logger.warning(f"Converted MP4 failed validation: {os.path.basename(final_path)}")
            return None
        else:
            logger.info(f"Converted to {os.path.basename(final_path)}")
        return final_path
    except Exception as e:
        logger.error(f"MP4 conversion failed: {e}")
        return None
