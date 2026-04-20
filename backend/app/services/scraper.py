import os
import logging
import httpx
import uuid
import asyncio
import random
import base64
import math
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import subprocess
import re
import imageio
import imageio_ffmpeg
from app.services.prompts import Prompts
from app.config import get_settings

settings = get_settings()
temp_storage_dir = settings.temp_storage_dir
os.makedirs(temp_storage_dir, exist_ok=True)

logger = logging.getLogger("VideoScraper")
logger.setLevel(logging.DEBUG)
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('[%(levelname)s] [Scraper] %(message)s'))
    logger.addHandler(handler)


DROPPED_TAGS = [
    "script", "style", "noscript", "svg", "canvas",
    "header", "footer", "nav", "aside",
    "form",
    "ads", "advertisement",
]
DROPPED_ATTRS = ["style", "onclick", "onload", "data-tracking"]


def clean_html(html: str) -> str:
    """
    Strip structural noise from raw HTML before sending to the LLM.
    """
    soup = BeautifulSoup(html, "html.parser")

    # Drop non-content tags
    for tag in soup.find_all(DROPPED_TAGS):
        tag.decompose()

    # Drop specific noise patterns
    for link in soup.find_all("link"):
        if not link or not hasattr(link, "attrs") or link.attrs is None:
            continue
        rel_attr = link.get("rel") or []
        rel = [r.lower() for r in rel_attr] if isinstance(rel_attr, list) else [str(rel_attr).lower()]
        # Drop prefetch, preconnect, icons, manifests, preloads, and cookie-related styles
        if any(x in str(rel) for x in ["prefetch", "preconnect", "icon", "manifest", "preload"]):
            link.decompose()
            continue
        if "stylesheet" in rel and any(s in str(link.get("href", "")) for s in ["base-min", "non-critical", "cookies-modal"]):
            link.decompose()

    for meta in soup.find_all("meta"):
        if any(x in str(meta.get("name", "")).lower() for x in ["msapplication", "theme-color", "viewport"]):
            meta.decompose()

    for hidden in soup.find_all("input", type="hidden"):
        hidden.decompose()

    # Drop specific ignored sections and patterns (cookies, modals, ad-detectors)
    ignore_classes_ids = ["video__preview", "about-chips-list", "cookies-modal", "ad-detector", "asg-"]
    for pattern in ignore_classes_ids:
        # Match classes
        for el in soup.find_all(class_=lambda x: x and pattern in x):
            el.decompose()
        # Match IDs
        for el in soup.find_all(id=lambda x: x and pattern in x):
            el.decompose()

    # Drop noisy attributes
    for tag in soup.find_all(True):
        for attr in DROPPED_ATTRS:
            tag.attrs.pop(attr, None)
        for attr in list(tag.attrs.keys()):
            if attr.startswith("data-") and attr not in ("data-src", "data-video-id", "data-duration"):
                del tag.attrs[attr]

    cleaned = str(soup)
    logger.debug(f"HTML cleaned: {len(html)} → {len(cleaned)} chars ({100*(1-len(cleaned)/max(len(html),1)):.0f}% reduction)")
    return cleaned


HEADLESS_OPTIONS = [
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--window-size=1920,1080",
    "--disable-search-engine-choice-screen",
    "--disable-blink-features=AutomationControlled",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--disable-extensions",
    "--disable-background-networking",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-default-apps",
    "--disable-hang-monitor",
    "--disable-prompt-on-repost",
    "--ignore-certificate-errors",
]

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:89.0) Gecko/20100101 Firefox/89.0",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/88.0.4324.96 Safari/537.36",
]


async def _safe_goto(page, target_url: str, timeout: int = 30000) -> None:
    """Navigate with one retry on ERR_SOCKET connection errors."""
    for attempt in range(2):
        try:
            await page.goto(target_url, wait_until="domcontentloaded", timeout=timeout)
            return
        except Exception as e:
            if attempt == 0 and "ERR_SOCKET" in str(e):
                logger.warning(f"Navigation socket error, retrying in 2s… ({e})")
                await asyncio.sleep(2)
            else:
                raise


async def _get_main_video_selector(page, llm_selector: str | None = None) -> str:
    """Identify the 'main' video. Uses the LLM-provided selector if valid, otherwise falls back to area heuristics."""
    if llm_selector:
        try:
            exists = await page.evaluate(f"() => !!document.querySelector({repr(llm_selector)})")
            if exists:
                logger.debug(f"Using LLM-provided main video selector: {llm_selector}")
                return llm_selector
        except Exception:
            pass

    return await page.evaluate('''() => {
        const videos = Array.from(document.querySelectorAll('video'));
        if (videos.length === 0) return 'video';
        if (videos.length === 1) return 'video';
        
        const sorted = videos.sort((a, b) => {
            const areaA = (a.offsetWidth || 0) * (a.offsetHeight || 0);
            const areaB = (b.offsetWidth || 0) * (b.offsetHeight || 0);
            return areaB - areaA;
        });
        
        const main = sorted[0];
        if (!main.id) main.id = 'vsearch-main-video-' + Math.random().toString(36).slice(2, 11);
        return '#' + main.id;
    }''')


async def _click_play(page, play_selector: str | None) -> bool:
    """
    Click the play button and verify if the video starts playing.
    Returns True if playback is detected, False if stuck.
    """
    FALLBACK_SELECTORS = [
        'button[aria-label*="play" i]',
        'button[title*="play" i]',
        '.ytp-play-button',
        '.vjs-big-play-button',
        '[class*="play-button"]',
        '[class*="playButton"]',
        'video',
    ]

    async def _js_click(selector: str) -> bool:
        try:
            return await page.evaluate(f'''() => {{
                const el = document.querySelector({repr(selector)});
                if (el) {{ el.click(); return true; }}
                return false;
            }}''')
        except Exception:
            return False

    # Try specific selector
    if play_selector and await _js_click(play_selector):
        logger.debug(f"Click succeeded on selector: {play_selector}")
    else:
        # Fall back through heuristic list
        for sel in FALLBACK_SELECTORS:
            if await _js_click(sel):
                logger.debug(f"Click succeeded on fallback: {sel}")
                break

    # Verify playback status
    await asyncio.sleep(2.0)
    is_playing = await page.evaluate('''() => {
        const videos = Array.from(document.querySelectorAll('video'));
        return videos.length > 0 && videos.some(v => !v.paused || v.currentTime > 0);
    }''')
    
    if is_playing:
        logger.info("Playback detected successfully.")
        return True
    
    logger.warning("Playback not detected after clicking. Interaction might be blocked.")
    return False


async def _resolve_interaction_stuck(page, llm_manager) -> bool:
    """Use Vision LLM to resolve a stuck state (e.g., ad overlays, age-gates)."""
    if not llm_manager:
        return False
        
    try:
        logger.info("Entering Vision Recovery Loop...")
        screenshot_bytes = await page.screenshot(type="jpeg", quality=80)
        screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
        
        # Save for debug
        try:
            with open(os.path.join(temp_storage_dir, "debug_stuck.jpg"), "wb") as f:
                f.write(screenshot_bytes)
            logger.info("Saved vision recovery screenshot to temp_storage/debug_stuck.jpg")
        except Exception:
            pass

        raw_html = await page.content()
        html_context = clean_html(raw_html)
        
        prompt = Prompts.resolve_stuck_vision(html_context)
        result = await llm_manager.call_vision(prompt, screenshot_b64)
        
        selector = result.get("action_selector")
        if selector:
            logger.info(f"Vision Agent suggested clicking: {selector}")
            await page.evaluate(f"() => {{ const el = document.querySelector({repr(selector)}); if (el) el.click(); }}")
            await asyncio.sleep(1.5)
            # Re-check playback
            return await page.evaluate('Array.from(document.querySelectorAll("video")).some(v => !v.paused)')
    except Exception as e:
        logger.error(f"Vision Recovery failed: {e}")
        
    return False


async def _set_quality(page, settings_selector: str | None, quality_selector: str | None) -> None:
    """
    Attempt to set video quality.
    Strictly optional and flexible for diverse UI layouts.
    """
    try:
        # 1. Click Settings Cog
        cog = settings_selector or '.ytp-settings-button'
        found_cog = await page.evaluate(f'''() => {{
            let el = document.querySelector({repr(cog)});
            if (!el) {{
                const btns = Array.from(document.querySelectorAll('button, [role="button"]'));
                el = btns.find(b => {{
                    const txt = (b.ariaLabel || b.title || b.textContent || "").toLowerCase();
                    return txt.includes('settings') || txt.includes('gear') || txt.includes('options');
                }});
            }}
            if (el) {{ el.click(); return true; }}
            return false;
        }}''')
        if not found_cog:
            return
        await asyncio.sleep(0.8)

        # 2. Click Quality Menu
        await page.evaluate('''() => {
            const items = Array.from(document.querySelectorAll('.ytp-menuitem, [role="menuitem"]'));
            const qItem = items.find(i => {
                const txt = i.textContent.toLowerCase();
                return txt.includes('quality') || txt.includes('resolution') || txt.includes('1080p') || txt.includes('720p');
            });
            if (qItem) qItem.click();
        }''')
        await asyncio.sleep(0.6)

        # 3. Select 720p or Highest
        await page.evaluate('''() => {
            const options = Array.from(document.querySelectorAll('.ytp-menuitem span, .vjs-menu-item, [role="menuitemradio"]'));
            const targets = ['720p', '1080p', '1440p', 'High'];
            for (const t of targets) {
                const found = options.find(o => o.textContent.includes(t));
                if (found) { found.click(); return true; }
            }
            if (options.length > 0) options[0].click(); // Fallback to first
        }''')
        logger.debug("Quality adjustment attempt finished.")
    except Exception as e:
        logger.debug(f"Quality adjustment optional step failed: {e}")


def _convert_to_mp4(webm_path: str) -> str:
    """
    Converts a webm file to mp4 using ffmpeg directly.
    Ensures audio is preserved and dimensions are even for browser compatibility.
    """
    if not os.path.exists(webm_path):
        return webm_path
    
    mp4_path = webm_path.replace(".webm", ".mp4")
    try:
        ffmpeg_exe = imageio_ffmpeg.get_ffmpeg_exe()
        logger.info(f"Converting {os.path.basename(webm_path)} to MP4 via ffmpeg…")
        
        # -vf scale="trunc(iw/2)*2:trunc(ih/2)*2" ensures even dimensions
        # -c:v libx264 for universal video codec
        # -c:a aac for universal audio codec
        # -pix_fmt yuv420p for web playback compatibility
        cmd = [
            ffmpeg_exe, "-y",
            "-i", webm_path,
            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
            "-c:v", "libx264",
            "-c:a", "aac",
            "-pix_fmt", "yuv420p",
            mp4_path
        ]
        
        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            logger.error(f"FFMPEG Error: {result.stderr}")
            # If conversion fails, keep webm but at least it didn't crash
            return webm_path
            
        # Clean up webm
        os.remove(webm_path)
        logger.info(f"Successfully converted to {os.path.basename(mp4_path)}")
        return mp4_path
    except Exception as e:
        logger.error(f"MP4 conversion failed: {e}")
        return webm_path


async def _is_forbidden(url: str, user_agent: str) -> bool:
    """Check if a URL returns 401 or 403 Forbidden."""
    if not url or url.startswith("blob:"):
        return True # Treat as forbidden to force fallback
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.head(url, headers={"User-Agent": user_agent}, follow_redirects=True)
            return resp.status_code in (401, 403, 404)
    except Exception:
        return False


async def run_extraction(
    url: str,
    user_agent: str,
    llm_manager=None,
    max_record_seconds: int = 10800,
) -> tuple[str, str, list[str], str, str]:
    """
    Async Playwright scraping pipeline.
    Returns: (html, screenshot_b64, network_video_urls, thumbnail_url, temp_video_url)
    """
    # Fetch raw HTML via httpx (non-blocking)
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

    temp_storage_dir = os.path.join(os.path.dirname(__file__), '..', '..', 'temp_storage')
    os.makedirs(temp_storage_dir, exist_ok=True)

    logger.debug(f"Starting extraction pipeline for: {url}")

    try:
        async with async_playwright() as p:
            logger.debug("Launching Playwright chromium browser (async)...")
            browser = await p.chromium.launch(headless=True, args=HEADLESS_OPTIONS)

            # ─────────────────────────────
            # FAST PASS  (metadata + network sniff)
            # ─────────────────────────────
            logger.debug("--- FAST PASS ---")
            context = await browser.new_context()
            page = await context.new_page()
            await page.set_extra_http_headers({"User-Agent": user_agent})

            video_urls: set[str] = set()

            def handle_request(request):
                r_url = request.url
                # Filter for video extensions
                if any(r_url.endswith(ext) for ext in [".mp4", ".m3u8", ".webm", ".mov", ".flv", ".avi"]):
                    lower_url = r_url.lower()
                    # Exclude HLS/DASH fragments (init files, segments, chunks)
                    # Patterns like _init_, _chunk_, seg-123.ts, or long numeric sequences in h264 streams
                    is_fragment = any(x in lower_url for x in ["_init_", "fragment", "chunk", "seg-", "/init"])
                    if not is_fragment:
                        # Also check for numbered segment patterns like _621_ or segment_1.mp4
                        if re.search(r'[_\-\/]\d{3,}[_\-\.]', lower_url):
                             is_fragment = True

                    if not is_fragment:
                        logger.debug(f"Network intercept (Primary): {r_url}")
                        video_urls.add(r_url)

            page.on("request", handle_request)

            logger.debug("Navigating (Fast Pass)…")
            await _safe_goto(page, url)

            # Let the page settle (non-blocking)
            await asyncio.sleep(1.5)

            # ── Stage 1: LLM vision + text navigation analysis ──
            play_selector: str | None = None
            fullscreen_selector: str | None = None
            main_video_selector: str | None = None
            direct_video_url_llm: str | None = None

            if llm_manager:
                logger.debug("Capturing screenshot for navigation vision analysis…")
                nav_screenshot_bytes = await page.screenshot(type="jpeg", quality=80)
                nav_screenshot_b64 = base64.b64encode(nav_screenshot_bytes).decode()
                
                # Save debug screenshot for navigation stage
                try:
                    with open(os.path.join(temp_storage_dir, "debug_nav.jpg"), "wb") as f:
                        f.write(nav_screenshot_bytes)
                    logger.info("Saved navigation screenshot to temp_storage/debug_nav.jpg")
                except Exception as e:
                    logger.warning(f"Failed to save debug navigation screenshot: {e}")

                logger.debug("Sending cleaned HTML and screenshot to LLM for navigation selectors…")
                raw_html = await page.content()
                pre_html = clean_html(raw_html)
                try:
                    logger.info("Invoking Vision LLM for navigation selectors...")
                    nav_map = await llm_manager.execute(Prompts.navigation_selectors_vision(pre_html), nav_screenshot_b64)
                    play_selector = nav_map.get("play_selector")
                    fullscreen_selector = nav_map.get("fullscreen_selector")
                    settings_selector = nav_map.get("settings_selector")
                    quality_selector = nav_map.get("quality_selector")
                    main_video_selector = nav_map.get("main_video_selector")
                    direct_video_url_llm = nav_map.get("direct_video_url")
                    logger.info(f"Agentic Vision Map: play=[{play_selector}], main_vid=[{main_video_selector}], direct_url=[{direct_video_url_llm}]")
                    # Set quality early if possible
                    await _set_quality(page, settings_selector, quality_selector)
                except Exception as e:
                    logger.warning(f"LLM navigation mapper failed: {e}")

            if direct_video_url_llm and direct_video_url_llm not in video_urls:
                video_urls.add(direct_video_url_llm)

            # ── Try to play the video ──
            await _click_play(page, play_selector)

            # ── Read DOM <video> src + duration ──
            try:
                dom_result = await page.evaluate('''() => {
                    const videos = Array.from(document.querySelectorAll('video'));
                    const srcs = videos.map(v =>
                        v.getAttribute('src') || v.currentSrc ||
                        (v.querySelector('source') ? v.querySelector('source').getAttribute('src') : null)
                    ).filter(s => s && !s.startsWith('blob:'));
                    const duration = videos.length > 0 ? (videos[0].duration || null) : null;
                    return { srcs, duration };
                }''')
                for dom_src in dom_result.get("srcs", []):
                    if dom_src.startswith("/"):
                        from urllib.parse import urljoin
                        dom_src = urljoin(url, dom_src)
                    logger.debug(f"DOM video src: {dom_src}")
                    video_urls.add(dom_src)

                raw_dur = dom_result.get("duration")
                if raw_dur and isinstance(raw_dur, (int, float)) and 0 < raw_dur < float('inf'):
                    dom_video_duration = float(raw_dur)
                    logger.info(f"DOM video duration: {dom_video_duration:.1f}s")
            except Exception as e:
                logger.debug(f"DOM video query failed: {e}")

            # Wait a tick for any late-loading video requests
            await page.mouse.move(random.randint(100, 500), random.randint(100, 500))
            await asyncio.sleep(1.5)

            logger.debug("Capturing screenshot…")
            screenshot_bytes = await page.screenshot(full_page=True)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
            
            # Save for debug inspection
            try:
                with open(os.path.join(temp_storage_dir, "debug_main.jpg"), "wb") as f:
                    f.write(screenshot_bytes)
                logger.info("Saved main screenshot to temp_storage/debug_main.jpg")
            except Exception as e:
                logger.warning(f"Failed to save debug screenshot: {e}")

            # ── Fallback Check: If the primary URL is Forbidden, force MediaRecorder ──
            raw_urls = list(video_urls)
            network_video_urls = []
            
            # Check the first few candidates for access. 
            # If the LLM suggested a direct URL, it's usually the first or prominent one.
            for cand_url in raw_urls[:3]:
                if await _is_forbidden(cand_url, user_agent):
                    logger.warning(f"Forbidden/Invalid URL detected: {cand_url}. Forcing MediaRecorder fallback.")
                    network_video_urls = [] 
                    break
                network_video_urls.append(cand_url)
            
            if network_video_urls:
                network_video_urls = raw_urls
                logger.debug(f"Fast Pass found {len(network_video_urls)} accessible video URL(s)")
            else:
                logger.debug("Fast Pass results empty or forbidden. Proceeding to Heavy Pass.")

            await context.close()

            # ── Compute actual recording length ──
            if dom_video_duration:
                actual_record_seconds = min(math.ceil(dom_video_duration), max_record_seconds)
            else:
                actual_record_seconds = max_record_seconds
            logger.info(f"Recording cap: {actual_record_seconds}s (dom={dom_video_duration}, hard_cap={max_record_seconds})")

            # ─────────────────────────────
            # HEAVY PASS  (MediaRecorder blob fallback)
            # ─────────────────────────────
            if not network_video_urls:
                logger.warning(f"No direct URLs accessible. Starting MediaRecorder Heavy Pass ({actual_record_seconds}s)…")
                heavy_context = await browser.new_context(accept_downloads=True)
                heavy_page = await heavy_context.new_page()
                await heavy_page.set_extra_http_headers({"User-Agent": user_agent})

                await _safe_goto(heavy_page, url)
                await asyncio.sleep(1)

                main_selector = await _get_main_video_selector(heavy_page, main_video_selector)
                logger.debug(f"Targeting main video: {main_selector}")

                # Agentic playback attempt
                if not await _click_play(heavy_page, play_selector):
                    # We are likely stuck by an overlay. Trigger Vision Recovery.
                    if await _resolve_interaction_stuck(heavy_page, llm_manager):
                        logger.info("Vision recovery Loop successful. Retrying playback.")
                        await _click_play(heavy_page, play_selector)

                if settings_selector or quality_selector:
                    await _set_quality(heavy_page, settings_selector, quality_selector)

                if fullscreen_selector:
                    try:
                        logger.debug(f"Clicking fullscreen selector: {fullscreen_selector}")
                        await heavy_page.click(fullscreen_selector, timeout=2000)
                    except Exception:
                        pass

                # Inject MediaRecorder (with requestFullscreen on main video)
                logger.debug("Injecting MediaRecorder…")
                await heavy_page.evaluate(f'''async () => {{
                    window.recorderChunks = [];
                    const video = document.querySelector({repr(main_selector)});
                    if (video) {{
                        try {{
                            if (video.requestFullscreen) {{
                                await video.requestFullscreen().catch(e => console.log('Fullscreen rejected', e));
                            }} else if (video.webkitRequestFullscreen) {{
                                await video.webkitRequestFullscreen().catch(e => console.log('Fullscreen rejected', e));
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

                logger.debug(f"Recording for {actual_record_seconds}s…")
                await asyncio.sleep(actual_record_seconds)

                logger.debug("Stopping MediaRecorder and triggering download…")
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
                        vid_path = os.path.join(temp_storage_dir, filename)
                        await download.save_as(vid_path)
                        
                        # Post-process: Convert to MP4
                        mp4_path = _convert_to_mp4(vid_path)
                        final_filename = os.path.basename(mp4_path)
                        
                        temp_video_url = f"http://localhost:8000/temp_storage/{final_filename}"
                        logger.debug(f"Final recording URL: {temp_video_url}")
                    else:
                        logger.error("MediaRecorder download was empty — likely DRM blocked.")
                except Exception as e:
                    logger.error(f"MediaRecorder Heavy Pass failed: {e}")

                await heavy_context.close()

            await browser.close()

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
