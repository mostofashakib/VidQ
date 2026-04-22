import asyncio
import base64
import logging
import math
import os
import random
import re
import threading
import uuid
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from app.config import get_settings
from app.services.scraper.html import clean_html
from app.services.scraper.media import (
    _is_ad_video_url,
    _is_forbidden,
    _get_main_playing_video_url,
    _download_video_direct,
    _convert_to_mp4,
)
from app.services.scraper.playback import (
    HEADLESS_OPTIONS,
    _safe_goto,
    _interruptible_sleep,
    _get_main_video_selector,
    _agentic_interact,
    _set_quality,
    _force_play_js,
    _try_click,
)
from app.services.prompts import Prompts

logger = logging.getLogger("VideoScraper")

_settings = get_settings()


async def run_extraction(
    url: str,
    user_agent: str,
    llm_manager=None,
    max_record_seconds: int = 10800,
    cancel_event: threading.Event | None = None,
) -> tuple[str, str, list[str], str, str]:
    """
    Async Playwright scraping pipeline.
    Returns: (html, screenshot_b64, network_video_urls, thumbnail_url, temp_video_url)
    """
    try:
        async with httpx.AsyncClient(timeout=12) as client:
            resp = await client.get(url, headers={"User-Agent": user_agent})
            resp.raise_for_status()
            html = resp.text
        soup = BeautifulSoup(html, "html.parser")
    except Exception as e:
        raise Exception(f"Failed to fetch page: {e}")

    screenshot_b64 = ""
    network_video_urls: list[str] = []
    temp_video_url: str | None = None
    dom_video_duration: float | None = None

    storage = _settings.temp_storage_dir
    os.makedirs(storage, exist_ok=True)

    logger.debug(f"Starting extraction pipeline for: {url}")

    try:
        async with async_playwright() as p:
            logger.debug("Launching Playwright chromium…")
            browser = await p.chromium.launch(headless=True, args=HEADLESS_OPTIONS)

            # ─────────────────────────────────────────
            # FAST PASS  (metadata + network sniff)
            # ─────────────────────────────────────────
            logger.debug("--- FAST PASS ---")
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_extra_http_headers({"User-Agent": user_agent})

            video_urls: set[str] = set()

            def handle_request(request):
                r_url = request.url
                if any(r_url.endswith(ext) for ext in [".mp4", ".m3u8", ".webm", ".mov", ".flv", ".avi"]):
                    if _is_ad_video_url(r_url):
                        logger.debug(f"Ad URL filtered: {r_url[:80]}")
                        return
                    lower_url = r_url.lower()
                    is_fragment = any(x in lower_url for x in ["_init_", "fragment", "chunk", "seg-", "/init"])
                    if not is_fragment and re.search(r'[_\-\/]\d{3,}[_\-\.]', lower_url):
                        is_fragment = True
                    if not is_fragment:
                        logger.debug(f"Network intercept: {r_url}")
                        video_urls.add(r_url)

            page.on("request", handle_request)

            logger.debug("Navigating (Fast Pass)…")
            await _safe_goto(page, url)
            await asyncio.sleep(1.5)

            # ── Stage 1: LLM vision navigation analysis ──
            play_selector: str | None = None
            fullscreen_selector: str | None = None
            settings_selector: str | None = None
            quality_selector: str | None = None
            main_video_selector: str | None = None
            direct_video_url_llm: str | None = None

            if llm_manager:
                logger.debug("Capturing screenshot for navigation vision analysis…")
                nav_bytes = await page.screenshot(type="jpeg", quality=80)
                nav_b64 = base64.b64encode(nav_bytes).decode()
                try:
                    with open(os.path.join(storage, "debug_nav.jpg"), "wb") as f:
                        f.write(nav_bytes)
                except Exception:
                    pass

                raw_html = await page.content()
                pre_html = clean_html(raw_html)
                try:
                    logger.info("Invoking LLM for navigation selectors…")
                    nav_map = await llm_manager.execute(Prompts.navigation_selectors_vision(pre_html), nav_b64)
                    play_selector = nav_map.get("play_selector")
                    fullscreen_selector = nav_map.get("fullscreen_selector")
                    settings_selector = nav_map.get("settings_selector")
                    quality_selector = nav_map.get("quality_selector")
                    main_video_selector = nav_map.get("main_video_selector")
                    direct_video_url_llm = nav_map.get("direct_video_url")
                    logger.info(f"Nav map: play=[{play_selector}], main=[{main_video_selector}], direct=[{direct_video_url_llm}]")
                    await _set_quality(page, settings_selector, quality_selector)
                except Exception as e:
                    logger.warning(f"LLM navigation mapper failed: {e}")

            if direct_video_url_llm:
                video_urls.add(direct_video_url_llm)

            # ── Try to trigger playback (agentic if LLM available) ──
            if llm_manager:
                await _agentic_interact(page, llm_manager, max_attempts=5)
            else:
                for sel in [play_selector, 'button[aria-label*="play" i]', '.vjs-big-play-button', 'video']:
                    if sel and await _try_click(page, sel):
                        break
                await asyncio.sleep(2.0)

            # ── Read DOM <video> src + duration ──
            main_dom_url: str | None = None
            try:
                dom_result = await page.evaluate('''() => {
                    const videos = Array.from(document.querySelectorAll('video'));
                    if (!videos.length) return { mainSrc: null, srcs: [], duration: null };
                    const sorted = videos.slice().sort((a, b) =>
                        (b.offsetWidth * b.offsetHeight) - (a.offsetWidth * a.offsetHeight));
                    const main = sorted[0];
                    const rawMain = main.currentSrc || main.getAttribute('src') || '';
                    const mainSrc = rawMain && !rawMain.startsWith('blob:') && rawMain.startsWith('http')
                        ? rawMain : null;
                    const allSrcs = videos.map(v =>
                        v.currentSrc || v.getAttribute('src') ||
                        (v.querySelector('source') ? v.querySelector('source').getAttribute('src') : null)
                    ).filter(s => s && !s.startsWith('blob:'));
                    return { mainSrc, srcs: allSrcs, duration: main.duration || null };
                }''')
                main_dom_url = dom_result.get("mainSrc")
                if main_dom_url:
                    logger.info(f"Main video DOM src (largest): {main_dom_url[:100]}")
                    video_urls.add(main_dom_url)
                for dom_src in dom_result.get("srcs", []):
                    if dom_src.startswith("/"):
                        dom_src = urljoin(url, dom_src)
                    if dom_src.startswith("http"):
                        logger.debug(f"DOM video src: {dom_src}")
                        video_urls.add(dom_src)
                raw_dur = dom_result.get("duration")
                if raw_dur and isinstance(raw_dur, (int, float)) and 0 < raw_dur < float('inf'):
                    dom_video_duration = float(raw_dur)
                    logger.info(f"DOM video duration: {dom_video_duration:.1f}s")
            except Exception as e:
                logger.debug(f"DOM video query failed: {e}")
            # Also check iframes (some players embed inside iframes)
            if not main_dom_url:
                main_dom_url = await _get_main_playing_video_url(page)
                if main_dom_url:
                    video_urls.add(main_dom_url)

            await page.mouse.move(random.randint(100, 500), random.randint(100, 500))
            await asyncio.sleep(1.5)

            logger.debug("Capturing main screenshot…")
            screenshot_bytes = await page.screenshot(full_page=True)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
            try:
                with open(os.path.join(storage, "debug_main.jpg"), "wb") as f:
                    f.write(screenshot_bytes)
            except Exception:
                pass

            # ── Identify main video URL and try direct download ──
            raw_urls = list(video_urls)
            clean_urls = [u for u in raw_urls if not _is_ad_video_url(u)]
            logger.info(f"Video URLs: {len(raw_urls)} raw → {len(clean_urls)} after ad filter")

            if main_dom_url and main_dom_url in clean_urls:
                ordered = [main_dom_url] + [u for u in clean_urls if u != main_dom_url]
            elif main_dom_url:
                ordered = [main_dom_url] + clean_urls
            else:
                ordered = clean_urls

            best_url: str | None = None
            for cand_url in ordered[:5]:
                if await _is_forbidden(cand_url, user_agent):
                    logger.warning(f"Forbidden URL (skipped): {cand_url[:80]}")
                    continue
                best_url = cand_url
                logger.info(f"Best video URL: {best_url[:100]}")
                break

            if best_url:
                dl_url = await _download_video_direct(best_url, url, user_agent)
                if dl_url:
                    temp_video_url = dl_url
                    logger.info("Fast Pass: ffmpeg download succeeded — video saved locally.")
                else:
                    logger.info("Fast Pass: ffmpeg failed — URL will be passed as-is.")
                network_video_urls = [best_url]
            else:
                network_video_urls = []
                logger.info("Fast Pass: no accessible video URL — proceeding to Heavy Pass.")

            await context.close()

            # ── Compute recording cap ──
            if dom_video_duration:
                actual_record_seconds = min(math.ceil(dom_video_duration), max_record_seconds)
            else:
                actual_record_seconds = max_record_seconds
            logger.info(f"Recording cap: {actual_record_seconds}s")

            # ─────────────────────────────────────────
            # HEAVY PASS  (MediaRecorder blob fallback)
            # ─────────────────────────────────────────
            if not network_video_urls:
                if cancel_event and cancel_event.is_set():
                    logger.info("Job cancelled before Heavy Pass.")
                    await browser.close()
                    raise asyncio.CancelledError("Job cancelled")

                logger.warning(f"Starting MediaRecorder Heavy Pass ({actual_record_seconds}s)…")
                heavy_context = await browser.new_context(accept_downloads=True)
                heavy_page = await heavy_context.new_page()
                await heavy_page.set_extra_http_headers({"User-Agent": user_agent})

                await _safe_goto(heavy_page, url)
                await asyncio.sleep(1)

                main_selector = await _get_main_video_selector(heavy_page, main_video_selector)
                logger.debug(f"Targeting main video: {main_selector}")

                playing = await _agentic_interact(heavy_page, llm_manager, max_attempts=5)
                if not playing:
                    playing = await _force_play_js(heavy_page)
                    if not playing:
                        logger.warning("Could not confirm playback — recording anyway (may capture blank stream).")

                if settings_selector or quality_selector:
                    await _set_quality(heavy_page, settings_selector, quality_selector)

                if fullscreen_selector:
                    try:
                        await heavy_page.click(fullscreen_selector, timeout=2000)
                    except Exception:
                        pass

                logger.info("Injecting MediaRecorder…")
                await heavy_page.evaluate(f'''async () => {{
                    window.recorderChunks = [];
                    const video = document.querySelector({repr(main_selector)});
                    if (video) {{
                        try {{
                            if (video.paused) {{
                                await video.play().catch(() => {{}});
                                await new Promise(r => setTimeout(r, 500));
                            }}
                            if (video.requestFullscreen) {{
                                await video.requestFullscreen().catch(() => {{}});
                            }} else if (video.webkitRequestFullscreen) {{
                                await video.webkitRequestFullscreen().catch(() => {{}});
                            }}
                            const stream = video.captureStream ? video.captureStream() : video.mozCaptureStream();
                            window.mediaRecorder = new MediaRecorder(stream, {{ mimeType: "video/webm" }});
                            window.mediaRecorder.ondataavailable = e => window.recorderChunks.push(e.data);
                            window.mediaRecorder.start();
                        }} catch (err) {{
                            console.error("Capture stream blocked", err);
                        }}
                    }}
                }}''')

                logger.debug(f"Recording for up to {actual_record_seconds}s (cancellable)…")
                was_cancelled = await _interruptible_sleep(actual_record_seconds, cancel_event)
                if was_cancelled:
                    logger.info("Recording cancelled by user request.")
                    await heavy_context.close()
                    await browser.close()
                    raise asyncio.CancelledError("Job cancelled during recording")

                logger.debug("Stopping MediaRecorder…")
                try:
                    async with heavy_page.expect_download(timeout=15000) as dl_info:
                        await heavy_page.evaluate('''() => {
                            if (window.mediaRecorder) {
                                window.mediaRecorder.stop();
                                setTimeout(() => {
                                    const blob = new Blob(window.recorderChunks, { type: "video/webm" });
                                    const a = document.createElement("a");
                                    a.href = URL.createObjectURL(blob);
                                    a.download = "recording.webm";
                                    document.body.appendChild(a);
                                    a.click();
                                }, 500);
                            } else {
                                const a = document.createElement("a");
                                a.href = "data:text/plain;base64,";
                                a.download = "failed.txt";
                                document.body.appendChild(a);
                                a.click();
                            }
                        }''')
                    download = await dl_info.value
                    if "recording.webm" in download.suggested_filename:
                        filename = f"{uuid.uuid4().hex}.webm"
                        vid_path = os.path.join(storage, filename)
                        await download.save_as(vid_path)
                        mp4_path = _convert_to_mp4(vid_path)
                        final_filename = os.path.basename(mp4_path)
                        temp_video_url = f"{_settings.base_url}/temp_storage/{final_filename}"
                        logger.debug(f"Final recording URL: {temp_video_url}")
                    else:
                        logger.error("MediaRecorder download empty — likely DRM blocked.")
                except Exception as e:
                    logger.error(f"MediaRecorder Heavy Pass failed: {e}")

                await heavy_context.close()

            await browser.close()

    except asyncio.CancelledError:
        raise
    except Exception as e:
        raise Exception(f"Playwright pipeline failed: {e}")

    # Extract thumbnail from static HTML
    thumbnail_url = None
    og_img = soup.find("meta", property="og:image", content=True)
    if og_img:
        thumbnail_url = og_img["content"]
    if not thumbnail_url:
        video_tag = soup.find("video", poster=True)
        if video_tag:
            thumbnail_url = video_tag.get("poster")
    if not thumbnail_url:
        thumbnail_url = f"data:image/png;base64,{screenshot_b64[:100000]}"

    return html, screenshot_b64, network_video_urls, thumbnail_url, temp_video_url or ""
