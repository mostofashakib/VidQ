import asyncio
import base64
import logging
import math
import os
import random
import re
import threading
import uuid
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup
from playwright.async_api import async_playwright

from app.config import get_settings
from app.services.scraper.html import clean_html
from app.services.scraper.media import (
    _is_ad_video_url,
    _is_forbidden,
    _get_main_playing_video_url,
    _detect_direct_video_embed,
    _try_ytdlp_on_page,
    _download_embed_video,
    _download_video_direct,
    _convert_to_mp4,
    _probe_file_duration,
)
from app.services.scraper.playback import (
    HEADLESS_OPTIONS,
    _safe_goto,
    _interruptible_sleep,
    _get_main_video_selector,
    _agentic_interact,
    _is_playing,
    _wait_for_playback_started,
    _set_quality,
    _force_play_js,
    _try_click,
    _wait_for_media_ready,
    _inject_stealth,
    _is_cloudflare_challenge,
    _bypass_cloudflare,
    _human_mouse_wander,
    _human_scroll,
)
from app.services.scraper.computer_use import ComputerUse
from app.services.prompts import Prompts

logger = logging.getLogger("VideoScraper")

_settings = get_settings()

_RECORD_DURATION_PAD_SECONDS = 2


def _parse_duration_seconds(raw_value) -> float | None:
    if raw_value is None:
        return None
    raw = str(raw_value).strip()
    if not raw:
        return None
    try:
        duration = float(raw)
        if 0 < duration < float("inf"):
            return duration
    except ValueError:
        pass
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", raw):
        parts = [float(part) for part in raw.split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    iso_match = re.fullmatch(
        r"P(?:T)?(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?",
        raw.upper(),
    )
    if iso_match:
        duration = (
            float(iso_match.group("hours") or 0) * 3600
            + float(iso_match.group("minutes") or 0) * 60
            + float(iso_match.group("seconds") or 0)
        )
        return duration if duration > 0 else None
    return None


def _extract_html_duration(soup: BeautifulSoup | None) -> float | None:
    if not soup:
        return None
    candidates = []
    video_tag = soup.find("video", duration=True)
    if video_tag:
        candidates.append(video_tag.get("duration"))
    for attrs in (
        {"property": "og:video:duration"},
        {"property": "video:duration"},
        {"name": "duration"},
        {"itemprop": "duration"},
    ):
        tag = soup.find("meta", attrs={**attrs, "content": True})
        if tag:
            candidates.append(tag.get("content"))
    for candidate in candidates:
        duration = _parse_duration_seconds(candidate)
        if duration:
            return duration
    return None

# Proxy-level errors that mean the proxy itself is broken — rotate to another
_PROXY_ERR_SIGNALS = (
    "ERR_INVALID_AUTH_CREDENTIALS",
    "ERR_TUNNEL_CONNECTION_FAILED",
    "ERR_PROXY_CONNECTION_FAILED",
    "ERR_SOCKS_CONNECTION_FAILED",
    "ERR_NO_SUPPORTED_PROXIES",
    "ERR_PROXY_AUTH_UNSUPPORTED",
)


async def _context_navigate_with_proxy_fallback(
    browser,
    url: str,
    proxy_pool: list[str],
    ctx_kwargs: dict,
    max_proxy_tries: int = 5,
) -> tuple:
    """
    Called only after a CF captcha fires on the initial direct (no-proxy) load.
    Tries up to `max_proxy_tries` random proxies, skipping any that fail with a
    proxy-level error.  Falls back to a direct connection on the final attempt so
    the job never hard-fails solely because of a bad proxy.
    Returns (context, page).
    """
    tried: set[str] = set()
    pool = list(proxy_pool)
    last_exc: Exception | None = None
    total = max(max_proxy_tries, 1)

    for attempt in range(total):
        is_last = attempt == total - 1
        untried = [p for p in pool if p not in tried]
        if not untried or is_last:
            proxy_cfg: dict | None = None
            is_last = True
        else:
            chosen = random.choice(untried)
            tried.add(chosen)
            proxy_cfg = {"server": chosen}

        label = proxy_cfg["server"][:60] if proxy_cfg else "direct (no proxy)"
        logger.info(f"CF bypass attempt {attempt + 1}/{total}: {label}")

        kwargs = dict(ctx_kwargs)
        if proxy_cfg:
            kwargs["proxy"] = proxy_cfg

        ctx = await browser.new_context(**kwargs)
        await _inject_stealth(ctx)
        pg = await ctx.new_page()

        try:
            await _safe_goto(pg, url)
            return ctx, pg
        except Exception as e:
            last_exc = e
            if not is_last and any(sig in str(e) for sig in _PROXY_ERR_SIGNALS):
                logger.warning(f"Proxy error, trying next ({str(e)[:80]})…")
                try:
                    await ctx.close()
                except Exception:
                    pass
                continue
            try:
                await ctx.close()
            except Exception:
                pass
            raise

    raise last_exc or RuntimeError(f"All proxy attempts exhausted for {url}")


async def run_extraction(
    url: str,
    user_agent: str,
    llm_manager=None,
    max_record_seconds: int = 10800,
    cancel_event: threading.Event | None = None,
    phase_callback=None,
    progress_callback=None,
) -> tuple[str, str, list[str], str, str]:
    """
    Async Playwright scraping pipeline.
    Returns: (html, screenshot_b64, network_video_urls, thumbnail_url, temp_video_url)
    """
    html = ""
    soup = None
    screenshot_b64 = ""
    network_video_urls: list[str] = []
    temp_video_url: str | None = None
    dom_video_duration: float | None = None

    storage = _settings.temp_storage_dir
    os.makedirs(storage, exist_ok=True)

    logger.debug(f"Starting extraction pipeline for: {url}")

    # ── Embedded-video fast-path (no Playwright, no LLM) ──
    embed_result = await _detect_direct_video_embed(url, user_agent)
    if embed_result:
        embedded_src, embed_html = embed_result
        dl_url = await _download_embed_video(embedded_src, referer=url)
        if dl_url:
            logger.info("Embedded fast-path succeeded — skipping Playwright and LLM.")
            embed_soup = BeautifulSoup(embed_html, "html.parser")
            embed_thumbnail = None
            og_img = embed_soup.find("meta", property="og:image", content=True)
            if og_img:
                embed_thumbnail = og_img["content"]
            if not embed_thumbnail:
                vt = embed_soup.find("video", poster=True)
                if vt:
                    embed_thumbnail = vt.get("poster")
            return embed_html, "", [embedded_src], embed_thumbnail or "", dl_url
        logger.warning("Embedded fast-path: download failed, falling through to Playwright.")

    # ── yt-dlp page fast-path ──────────────────────────────────────────────────
    # Many video hosting platforms have yt-dlp
    # extractors that know how to decode obfuscated player JS, refresh HLS tokens,
    # and handle cookie-based auth.  For unsupported sites yt-dlp fails within a
    # few seconds with "Unsupported URL", so this adds minimal latency.
    ytdlp_pre = await _try_ytdlp_on_page(url, user_agent, timeout_s=90)
    if ytdlp_pre:
        logger.info("yt-dlp page fast-path succeeded — skipping Playwright.")
        return "", "", [url], "", ytdlp_pre
    logger.debug("yt-dlp page fast-path: no result, proceeding to Playwright.")

    # Resolve persistent cookie storage path
    profile_dir = _settings.browser_profile_dir
    os.makedirs(profile_dir, exist_ok=True)
    state_file = os.path.join(profile_dir, "storage_state.json")

    proxy_pool = _settings.proxy_urls  # may be empty list

    try:
        async with async_playwright() as p:
            logger.debug(f"Launching bundled Chromium ({'headless' if _settings.browser_headless else 'headed'})")
            browser = await p.chromium.launch(
                headless=_settings.browser_headless,
                args=HEADLESS_OPTIONS,
            )

            # ─────────────────────────────────────────
            # FAST PASS  (metadata + network sniff)
            # ─────────────────────────────────────────
            logger.debug("--- FAST PASS ---")
            if phase_callback:
                phase_callback("fast_pass")

            # Load persistent storage state (cookies/localStorage) so the browser
            # looks like a returning human visitor that has already accepted consent
            # banners, passed bot checks, and has a browsing history.
            _storage_state = state_file if os.path.exists(state_file) else None
            context = await browser.new_context(
                user_agent=user_agent,
                locale="en-US",
                timezone_id="America/New_York",
                viewport={"width": 1920, "height": 1080},
                storage_state=_storage_state,
            )
            # Inject stealth patches on the context so all pages + frames are covered
            await _inject_stealth(context)
            page = await context.new_page()

            video_urls: set[str] = set()

            def handle_request(request):
                r_url = request.url
                # Use path-only matching so query-string tokens don't break extension checks
                # e.g. https://cdn.example.com/video.m3u8?token=abc  →  path ends in .m3u8
                r_path = urlparse(r_url).path.lower()
                if any(r_path.endswith(ext) for ext in [".mp4", ".m3u8", ".webm", ".mov", ".flv", ".avi"]):
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

            def handle_response(response):
                # Catch video URLs by content-type — covers HLS manifests and direct
                # video files that don't have a standard extension in their path.
                try:
                    r_url = response.url
                    if _is_ad_video_url(r_url) or r_url.startswith("blob:"):
                        return
                    ct = (response.headers.get("content-type") or "").lower().split(";")[0].strip()
                    if ct in ("application/x-mpegurl", "application/vnd.apple.mpegurl",
                              "application/dash+xml"):
                        logger.debug(f"Manifest (content-type={ct}): {r_url[:80]}")
                        video_urls.add(r_url)
                    elif ct.startswith("video/"):
                        cl = int(response.headers.get("content-length") or 0)
                        if cl > 100_000:
                            logger.debug(f"Video response ({ct}, {cl // 1024}KB): {r_url[:80]}")
                            video_urls.add(r_url)
                except Exception:
                    pass

            page.on("request", handle_request)
            page.on("response", handle_response)

            logger.debug("Navigating (Fast Pass)…")
            await _safe_goto(page, url)
            await asyncio.sleep(random.uniform(1.5, 2.5))

            # ── Cloudflare challenge: clear session + rotate proxy + retry ──────
            if await _is_cloudflare_challenge(page):
                logger.info("Fast Pass: CF challenge detected — clearing cookies, rotating proxy, retrying…")

                # 1. Discard the challenged context and any cached state
                try:
                    await context.close()
                except Exception:
                    pass
                try:
                    if os.path.exists(state_file):
                        os.remove(state_file)
                        logger.debug("Storage state cleared (fresh session for retry).")
                except Exception:
                    pass

                # 2. New context: one proxy attempt (triggered by the CF captcha above),
                #    falls back to direct if the proxy itself is broken
                context, page = await _context_navigate_with_proxy_fallback(
                    browser,
                    url,
                    proxy_pool,
                    {
                        "user_agent": user_agent,
                        "locale": "en-US",
                        "timezone_id": "America/New_York",
                        "viewport": {"width": 1920, "height": 1080},
                    },
                )
                page.on("request", handle_request)
                page.on("response", handle_response)
                await asyncio.sleep(random.uniform(1.5, 2.5))

                # 3. Run click-based bypass steps (auto-wait → DOM click → LLM → heuristics)
                cf_ok = await _bypass_cloudflare(page, llm_manager)
                if not cf_ok:
                    logger.warning("Fast Pass: CF challenge persists after rotate + bypass — continuing anyway.")
                await asyncio.sleep(random.uniform(1.0, 2.0))
            else:
                await _human_mouse_wander(page)
                await _human_scroll(page, n=1)
                await asyncio.sleep(random.uniform(0.8, 1.8))

            html = await page.content()
            soup = BeautifulSoup(html, "html.parser")
            html_video_duration = _extract_html_duration(soup)
            if html_video_duration:
                dom_video_duration = html_video_duration
                logger.info(f"HTML video duration metadata: {dom_video_duration:.1f}s")

            # ── Stage 1: LLM vision navigation analysis ──
            play_selector: str | None = None
            fullscreen_selector: str | None = None
            settings_selector: str | None = None
            quality_selector: str | None = None
            main_video_selector: str | None = None
            direct_video_url_llm: str | None = None

            if llm_manager:
                logger.debug("Capturing screenshot for navigation vision analysis…")
                nav_bytes = await ComputerUse(page).screenshot(quality=80)
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
                    llm_duration = _parse_duration_seconds(nav_map.get("duration"))
                    if llm_duration:
                        dom_video_duration = llm_duration
                        logger.info(f"LLM navigation duration: {dom_video_duration:.1f}s")
                    logger.info(f"Nav map: play=[{play_selector}], main=[{main_video_selector}], direct=[{direct_video_url_llm}]")
                    await _set_quality(page, settings_selector, quality_selector)
                except Exception as e:
                    logger.warning(f"LLM navigation mapper failed: {e}")

            if direct_video_url_llm:
                video_urls.add(direct_video_url_llm)

            # ── Try to trigger playback (agentic if LLM available) ──
            if llm_manager:
                if await _is_playing(page):
                    logger.info("Fast Pass: video auto-playing — skipping agentic interact.")
                else:
                    await _agentic_interact(page, llm_manager, max_attempts=5)
                # Give the player time to request the video stream after clicking play.
                # HLS/DASH manifests triggered by the click will appear in network_urls
                # only if we wait for the XHR to complete before reading video_urls.
                await asyncio.sleep(3.0)
            else:
                for sel in [play_selector, 'button[aria-label*="play" i]', '.vjs-big-play-button', 'video']:
                    if sel and await _try_click(page, sel):
                        break
                await asyncio.sleep(3.0)

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

            cu = ComputerUse(page)
            await cu.mouse_move(random.randint(100, 500), random.randint(100, 500))
            await asyncio.sleep(1.5)

            logger.debug("Capturing main screenshot…")
            screenshot_bytes = await cu.screenshot(quality=85, full_page=True)
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

            for cand_url in ordered[:8]:
                if await _is_forbidden(cand_url, user_agent):
                    logger.warning(f"Forbidden URL (skipped): {cand_url[:80]}")
                    continue

                logger.info(f"Trying candidate: {cand_url[:100]}")
                dl_url = await _download_video_direct(
                    cand_url, url, user_agent,
                    total_duration_s=dom_video_duration,
                    progress_callback=progress_callback,
                )
                if not dl_url:
                    logger.info("ffmpeg failed for candidate, trying next.")
                    continue

                # Duration guard: reject downloaded file if it looks like a pre-roll ad.
                # A candidate whose duration is less than half the page-reported duration
                # is almost certainly an ad that loaded before the main video stream.
                if dom_video_duration and dom_video_duration > 30:
                    filename = dl_url.rstrip("/").split("/")[-1]
                    local_path = os.path.join(storage, filename)
                    if os.path.exists(local_path):
                        dl_duration = _probe_file_duration(local_path)
                        if dl_duration is not None and dl_duration < dom_video_duration * 0.5:
                            logger.warning(
                                f"Duration mismatch: downloaded {dl_duration:.0f}s but page"
                                f" reports ~{dom_video_duration:.0f}s — likely a pre-roll ad, discarding"
                            )
                            try:
                                os.remove(local_path)
                            except Exception:
                                pass
                            continue

                temp_video_url = dl_url
                network_video_urls = [cand_url]
                logger.info(f"Fast Pass: download accepted — {dl_url.split('/')[-1]}")
                break

            if not network_video_urls:
                logger.info("Fast Pass: no valid video URL found via network intercept.")
                # yt-dlp fallback: the network interceptor captured a URL but the
                # direct download failed (tokenised URL, auth headers, etc.).  yt-dlp
                # re-fetches the page and gets fresh tokens — try it on the original
                # page URL before committing to the slow MediaRecorder heavy pass.
                logger.info("Fast Pass: trying yt-dlp on original page URL as fallback…")
                ytdlp_fallback = await _try_ytdlp_on_page(url, user_agent, timeout_s=90)
                if ytdlp_fallback:
                    logger.info("yt-dlp fallback succeeded — skipping Heavy Pass.")
                    temp_video_url = ytdlp_fallback
                    network_video_urls = [url]
                else:
                    logger.info("yt-dlp fallback: no result — proceeding to Heavy Pass.")

            # Persist cookies/localStorage for future sessions
            try:
                await context.storage_state(path=state_file)
                logger.debug("Browser storage state saved.")
            except Exception as _e:
                logger.debug(f"Could not save storage state: {_e}")

            await context.close()

            # ── Compute recording cap ──
            if dom_video_duration:
                actual_record_seconds = min(
                    math.ceil(dom_video_duration + _RECORD_DURATION_PAD_SECONDS),
                    max_record_seconds,
                )
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
                _storage_state_heavy = state_file if os.path.exists(state_file) else None
                heavy_context = await browser.new_context(
                    accept_downloads=True,
                    user_agent=user_agent,
                    locale="en-US",
                    timezone_id="America/New_York",
                    viewport={"width": 1920, "height": 1080},
                    storage_state=_storage_state_heavy,
                )
                await _inject_stealth(heavy_context)
                heavy_page = await heavy_context.new_page()

                await _safe_goto(heavy_page, url)
                await asyncio.sleep(random.uniform(1.2, 2.2))

                # ── Cloudflare challenge: clear session + rotate proxy + retry ──
                if await _is_cloudflare_challenge(heavy_page):
                    logger.info("Heavy Pass: CF challenge detected — clearing cookies, rotating proxy, retrying…")

                    # 1. Discard challenged context and cached state
                    try:
                        await heavy_context.close()
                    except Exception:
                        pass
                    try:
                        if os.path.exists(state_file):
                            os.remove(state_file)
                            logger.debug("Storage state cleared (fresh session for heavy pass retry).")
                    except Exception:
                        pass

                    # 2. New context: one proxy attempt (triggered by the CF captcha above),
                    #    falls back to direct if the proxy itself is broken
                    heavy_context, heavy_page = await _context_navigate_with_proxy_fallback(
                        browser,
                        url,
                        proxy_pool,
                        {
                            "accept_downloads": True,
                            "user_agent": user_agent,
                            "locale": "en-US",
                            "timezone_id": "America/New_York",
                            "viewport": {"width": 1920, "height": 1080},
                        },
                    )
                    await asyncio.sleep(random.uniform(1.5, 2.5))

                    # 3. Click-based bypass (auto-wait → DOM click → LLM → heuristics)
                    cf_ok = await _bypass_cloudflare(heavy_page, llm_manager)
                    if not cf_ok:
                        logger.warning("Heavy Pass: CF challenge persists after rotate + bypass — continuing anyway.")
                    await asyncio.sleep(random.uniform(1.0, 2.0))
                else:
                    await _human_mouse_wander(heavy_page)

                if phase_callback:
                    phase_callback("heavy_pass_waiting")
                logger.info("Heavy Pass: polling for media readiness (up to 15s)…")
                media_ready = await _wait_for_media_ready(heavy_page, timeout_s=15)
                if media_ready:
                    logger.info("Heavy Pass: media ready (play button / video visible).")
                else:
                    logger.info("Heavy Pass: media readiness timeout — proceeding anyway.")

                main_selector = await _get_main_video_selector(heavy_page, main_video_selector)
                logger.debug(f"Targeting main video: {main_selector}")

                # After page has settled, take HTML → LLM for fresh selectors
                if llm_manager:
                    try:
                        logger.info("Heavy Pass: capturing HTML for LLM navigation analysis…")
                        raw_html = await heavy_page.content()
                        pre_html = clean_html(raw_html)
                        heavy_nav_bytes = await ComputerUse(heavy_page).screenshot(quality=80)
                        heavy_nav_b64 = base64.b64encode(heavy_nav_bytes).decode()
                        heavy_nav_map = await llm_manager.execute(
                            Prompts.navigation_selectors_vision(pre_html), heavy_nav_b64
                        )
                        if heavy_nav_map.get("play_selector"):
                            play_selector = heavy_nav_map["play_selector"]
                            logger.info(f"Heavy Pass: updated play_selector=[{play_selector}]")
                        if heavy_nav_map.get("main_video_selector"):
                            main_video_selector = heavy_nav_map["main_video_selector"]
                            main_selector = await _get_main_video_selector(heavy_page, main_video_selector)
                            logger.info(f"Heavy Pass: updated main_selector=[{main_selector}]")
                        llm_duration = _parse_duration_seconds(heavy_nav_map.get("duration"))
                        if llm_duration:
                            dom_video_duration = llm_duration
                            duration_cap = min(
                                math.ceil(dom_video_duration + _RECORD_DURATION_PAD_SECONDS),
                                max_record_seconds,
                            )
                            if duration_cap < actual_record_seconds:
                                logger.info(
                                    f"Heavy Pass: LLM navigation duration={dom_video_duration:.1f}s; "
                                    f"updating recording cap from {actual_record_seconds}s to {duration_cap}s."
                                )
                                actual_record_seconds = duration_cap
                            else:
                                logger.info(f"Heavy Pass: LLM navigation duration={dom_video_duration:.1f}s")
                    except Exception as e:
                        logger.warning(f"Heavy Pass LLM navigation analysis failed: {e}")

                if await _is_playing(heavy_page):
                    logger.info("Heavy Pass: video already playing (autoplay) — skipping agentic interact.")
                    playing = True
                else:
                    playing = await _agentic_interact(heavy_page, llm_manager, max_attempts=5)
                    if not playing:
                        playing = await _force_play_js(heavy_page)
                        if not playing:
                            logger.error("Could not confirm playback — aborting recording to avoid blank capture.")
                            await heavy_context.close()
                            await browser.close()
                            raise RuntimeError("Playback could not be confirmed; recording aborted.")

                if settings_selector or quality_selector:
                    await _set_quality(heavy_page, settings_selector, quality_selector)

                if fullscreen_selector:
                    try:
                        logger.info(f"[Agent] Clicking provided fullscreen selector: {fullscreen_selector!r}")
                        await heavy_page.click(fullscreen_selector, timeout=2000)
                    except Exception:
                        logger.info(f"[Agent] Provided fullscreen selector click failed: {fullscreen_selector!r}")

                if not await _wait_for_playback_started(heavy_page, timeout_s=5):
                    logger.error("Playback-start detector failed before MediaRecorder injection.")
                    await heavy_context.close()
                    await browser.close()
                    raise RuntimeError("Playback could not be confirmed before recording.")

                # Find the frame that actually holds the video element.
                # Streaming platforms embed their player
                # inside an <iframe>; calling captureStream() must happen inside
                # that frame's execution context, not the parent page.
                target_frame = heavy_page
                for frame in heavy_page.frames[1:]:
                    try:
                        if await frame.evaluate("() => document.querySelectorAll('video').length > 0"):
                            target_frame = frame
                            logger.info(f"Heavy Pass: video located in iframe — {frame.url[:60]}")
                            break
                    except Exception:
                        pass
                main_selector = await _get_main_video_selector(target_frame, main_video_selector)
                logger.debug(f"MediaRecorder target: {'iframe' if target_frame is not heavy_page else 'main'} / {main_selector}")

                try:
                    target_duration = await target_frame.evaluate(f'''() => {{
                        const video = document.querySelector({repr(main_selector)});
                        if (!video || !Number.isFinite(video.duration) || video.duration <= 0) return null;
                        return video.duration;
                    }}''')
                    if target_duration and isinstance(target_duration, (int, float)):
                        target_duration = float(target_duration)
                        duration_cap = min(
                            math.ceil(target_duration + _RECORD_DURATION_PAD_SECONDS),
                            max_record_seconds,
                        )
                        if duration_cap < actual_record_seconds:
                            logger.info(
                                f"[MediaRecorder] Updating recording cap from {actual_record_seconds}s "
                                f"to {duration_cap}s based on active player duration "
                                f"({target_duration:.1f}s)."
                            )
                            actual_record_seconds = duration_cap
                            dom_video_duration = target_duration
                        else:
                            logger.info(
                                f"[MediaRecorder] Active player duration detected: "
                                f"{target_duration:.1f}s (recording cap remains {actual_record_seconds}s)."
                            )
                except Exception as duration_err:
                    logger.debug(f"[MediaRecorder] Active player duration check failed: {duration_err}")

                progress_record_seconds = (
                    max(1, math.ceil(dom_video_duration))
                    if dom_video_duration
                    else actual_record_seconds
                )
                if dom_video_duration:
                    logger.info(
                        f"[MediaRecorder] Frontend progress duration: "
                        f"{progress_record_seconds}s from detected video duration "
                        f"({dom_video_duration:.1f}s); recording cap is {actual_record_seconds}s."
                    )
                if phase_callback:
                    phase_callback("heavy_pass_recording", recording_duration=progress_record_seconds)

                # Log video state just before injecting so we know what we're recording
                try:
                    pre_state = await target_frame.evaluate(f'''() => {{
                        const v = document.querySelector({repr(main_selector)});
                        if (!v) return {{ found: false }};
                        return {{
                            found: true, paused: v.paused,
                            currentTime: v.currentTime,
                            readyState: v.readyState,
                            src: (v.currentSrc || '').slice(0, 80),
                        }};
                    }}''')
                    if pre_state.get('found'):
                        logger.info(
                            f"[MediaRecorder] Pre-injection video state: "
                            f"paused={pre_state.get('paused')}, "
                            f"currentTime={pre_state.get('currentTime', 0):.2f}s, "
                            f"readyState={pre_state.get('readyState')}, "
                            f"src={pre_state.get('src')!r}"
                        )
                    else:
                        logger.warning(
                            f"[MediaRecorder] Video element NOT found with selector {main_selector!r} "
                            f"in {'iframe' if target_frame is not heavy_page else 'main frame'}"
                        )
                except Exception as _pre_err:
                    logger.debug(f"[MediaRecorder] Pre-injection state check failed: {_pre_err}")

                logger.info(
                    f"[MediaRecorder] Injecting recorder — "
                    f"frame: {'iframe (' + target_frame.url[:50] + ')' if target_frame is not heavy_page else 'main frame'}, "
                    f"selector: {main_selector!r}"
                )
                await target_frame.evaluate(f'''async () => {{
                    window.recorderChunks = [];
                    window.mediaRecorderError = null;
                    window.mediaRecorderStats = {{ started: false, mimeType: null, trackCount: 0, videoTrackCount: 0 }};
                    const video = document.querySelector({repr(main_selector)});
                    if (!video) {{
                        window.mediaRecorderError = "video-element-not-found";
                    }} else {{
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
                            const tracks = stream ? stream.getTracks() : [];
                            const videoTracks = stream ? stream.getVideoTracks() : [];
                            window.mediaRecorderStats.trackCount = tracks.length;
                            window.mediaRecorderStats.videoTrackCount = videoTracks.length;
                            if (!stream || !videoTracks.length) {{
                                throw new Error("captureStream returned no video tracks");
                            }}
                            const mimeTypes = [
                                "video/webm;codecs=vp8,opus",
                                "video/webm;codecs=vp8",
                                "video/webm;codecs=vp9",
                                "video/webm",
                            ];
                            const mimeType = mimeTypes.find(type =>
                                !window.MediaRecorder ||
                                !MediaRecorder.isTypeSupported ||
                                MediaRecorder.isTypeSupported(type)
                            );
                            const options = mimeType ? {{ mimeType }} : undefined;
                            window.mediaRecorderStats.mimeType = mimeType || "browser-default";
                            window.mediaRecorder = new MediaRecorder(stream, options);
                            window.mediaRecorder.ondataavailable = e => {{
                                if (e.data && e.data.size > 0) window.recorderChunks.push(e.data);
                            }};
                            window.mediaRecorder.onerror = e => {{
                                window.mediaRecorderError = e && e.error ? e.error.message : "mediarecorder-error";
                            }};
                            window.mediaRecorder.start(1000);
                            window.mediaRecorderStats.started = true;
                        }} catch (err) {{
                            window.mediaRecorderError = err && err.message ? err.message : String(err);
                            console.error("Capture stream blocked", err);
                        }}
                    }}
                }}''')

                logger.debug(f"Recording for up to {actual_record_seconds}s with stuck-detection…")
                _CHECK_INTERVAL = 30      # seconds between health checks
                _MIN_SIZE_GROWTH = 100_000  # 100 KB/30 s minimum for real video (canvas fallback)
                _FRAME_EVAL_START = 30    # baseline frame continuity on the first 30s check.
                _elapsed = 0
                _frame_strikes = 0        # consecutive checks with identical frame hash
                _time_strikes = 0         # consecutive checks with non-advancing currentTime
                _prev_video_time: float | None = None
                _prev_frame_hash: int | None = None
                _prev_total_size: int | None = None
                was_cancelled = False
                was_stuck = False

                while _elapsed < actual_record_seconds:
                    if cancel_event and cancel_event.is_set():
                        was_cancelled = True
                        break
                    _tick = min(_CHECK_INTERVAL, actual_record_seconds - _elapsed)
                    await asyncio.sleep(_tick)
                    _elapsed += _tick
                    if cancel_event and cancel_event.is_set():
                        was_cancelled = True
                        break

                    # Health check: is the video playing real content?
                    # Primary:  32×32 canvas pixel hash — detects static image vs real video.
                    # Fallback: chunk size growth rate — used when canvas is cross-origin blocked.
                    # Also:     currentTime advancement — diagnostic only; frame changes decide stuck.
                    try:
                        health = await target_frame.evaluate(f'''() => {{
                            const chunks = window.recorderChunks || [];
                            const totalSize = chunks.reduce((s, b) => s + b.size, 0);
                            const v = document.querySelector({repr(main_selector)});
                            const info = {{
                                chunkCount: chunks.length,
                                totalSize,
                                currentTime: v ? v.currentTime : -1,
                                paused: v ? v.paused : true,
                                frameHash: null,
                                frameHashOk: false,
                            }};
                            if (!v || !v.videoWidth || !v.videoHeight) return info;
                            try {{
                                const canvas = document.createElement('canvas');
                                canvas.width = 32; canvas.height = 32;
                                const ctx = canvas.getContext('2d');
                                ctx.drawImage(v, 0, 0, 32, 32);
                                const d = ctx.getImageData(0, 0, 32, 32).data;
                                let hash = 2166136261;
                                for (let i = 0; i < d.length; i += 4) {{
                                    hash ^= d[i];
                                    hash = Math.imul(hash, 16777619);
                                    hash ^= d[i + 1];
                                    hash = Math.imul(hash, 16777619);
                                    hash ^= d[i + 2];
                                    hash = Math.imul(hash, 16777619);
                                }}
                                info.frameHash = hash >>> 0;
                                info.frameHashOk = true;
                            }} catch (_) {{
                                // SecurityError when video source is cross-origin; handled in Python
                            }}
                            return info;
                        }}''')

                        chunk_count = int(health.get("chunkCount", 0))
                        total_size = int(health.get("totalSize", 0))
                        current_time = float(health.get("currentTime", -1))
                        frame_hash = health.get("frameHash")
                        frame_hash_ok = bool(health.get("frameHashOk", False))

                        logger.info(
                            f"[MediaRecorder] t={_elapsed}s: "
                            f"chunks={chunk_count}, size={total_size // 1024}KB, "
                            f"ct={current_time:.2f}s, "
                            f"paused={health.get('paused')}, "
                            f"canvas={'ok (hash=' + str(frame_hash) + ')' if frame_hash_ok else 'blocked'}"
                        )

                        # If video is paused mid-recording, attempt a silent resume
                        if health.get("paused") and current_time > 0:
                            logger.warning(
                                f"[MediaRecorder] Video paused at t={current_time:.2f}s — attempting resume"
                            )
                            try:
                                await target_frame.evaluate(f'''async () => {{
                                    const v = document.querySelector({repr(main_selector)});
                                    if (v && v.paused) await v.play().catch(() => {{}});
                                }}''')
                            except Exception:
                                pass

                        if dom_video_duration and _elapsed >= max(1, dom_video_duration):
                            logger.info(
                                f"[MediaRecorder] Recorded {_elapsed}s, matching video duration "
                                f"~{dom_video_duration:.1f}s — stopping and saving."
                            )
                            break

                        if (
                            dom_video_duration
                            and _prev_video_time is not None
                            and _prev_video_time >= max(5.0, dom_video_duration * 0.75)
                            and current_time >= 0
                            and current_time + 5.0 < _prev_video_time
                        ):
                            logger.info(
                                f"[MediaRecorder] Video loop detected "
                                f"(currentTime {current_time:.2f}s after {_prev_video_time:.2f}s) — "
                                "stopping and saving completed recording."
                            )
                            break

                        # No chunks can be normal until a timeslice is emitted; frame checks decide stuck.
                        if chunk_count == 0 and _elapsed >= _CHECK_INTERVAL:
                            logger.warning(
                                "[MediaRecorder] No chunks emitted yet; continuing because frame-change "
                                "detection is the primary stuck signal."
                            )

                        # ── Frame content check (primary) ──────────────────────────────
                        # Baseline at the first 30s check, then treat unchanged frames on
                        # later 30s checks as stuck strikes.
                        if frame_hash_ok:
                            if _elapsed >= _FRAME_EVAL_START:
                                if _prev_frame_hash is None:
                                    logger.info(f"[MediaRecorder] Frame baseline set (hash={frame_hash}).")
                                elif frame_hash == _prev_frame_hash:
                                    _frame_strikes += 1
                                    logger.warning(
                                        f"[MediaRecorder] Frame unchanged (hash={frame_hash}, "
                                        f"strike {_frame_strikes}/3) — possible static image."
                                    )
                                    if _frame_strikes >= 3:
                                        logger.warning(
                                            "[MediaRecorder] Static frame confirmed (3 consecutive "
                                            f"{_CHECK_INTERVAL}s checks) — aborting (recording an image, not a video)."
                                        )
                                        was_stuck = True
                                        break
                                else:
                                    if _frame_strikes > 0:
                                        logger.info(f"[MediaRecorder] Frame changed — resetting strike counter (was {_frame_strikes})")
                                    _frame_strikes = 0
                                _prev_frame_hash = frame_hash
                            else:
                                logger.info(
                                    f"[MediaRecorder] Frame hash grace period "
                                    f"({_elapsed}s < {_FRAME_EVAL_START}s) — deferring baseline"
                                )

                        # ── Size growth fallback (when canvas is cross-origin blocked) ─
                        else:
                            if _elapsed >= _FRAME_EVAL_START and _prev_total_size is not None:
                                growth = total_size - _prev_total_size
                                if growth < _MIN_SIZE_GROWTH:
                                    _frame_strikes += 1
                                    logger.warning(
                                        f"[MediaRecorder] Low size growth "
                                        f"({growth // 1024}KB/{_CHECK_INTERVAL}s, "
                                        f"strike {_frame_strikes}/3)."
                                    )
                                    if _frame_strikes >= 3:
                                        logger.warning(
                                            "[MediaRecorder] Insufficient data growth (3 consecutive) — aborting "
                                            "(likely static/black screen)."
                                        )
                                        was_stuck = True
                                        break
                                else:
                                    if _frame_strikes > 0:
                                        logger.info(f"[MediaRecorder] Size growth recovered — resetting strike counter (was {_frame_strikes})")
                                    _frame_strikes = 0

                        _prev_total_size = total_size  # always track for absolute-size reference

                        # ── currentTime advancement check ──────────────────────────────
                        # Diagnostic only. Some players expose unreliable currentTime while
                        # frames are still changing, so this does not mark the recording stuck.
                        if (current_time > 1.0 and _prev_video_time is not None
                                and current_time <= _prev_video_time + 0.5):
                            _time_strikes += 1
                            logger.warning(
                                f"[MediaRecorder] Video not advancing "
                                f"(ct={current_time:.2f}s was {_prev_video_time:.2f}s, "
                                f"diagnostic strike {_time_strikes})."
                            )
                        elif current_time > 1.0:
                            if _time_strikes > 0:
                                logger.info(f"[MediaRecorder] Video advancing again — resetting time-strike counter (was {_time_strikes})")
                            _time_strikes = 0

                        if current_time >= 0:
                            _prev_video_time = current_time

                    except Exception as e:
                        logger.debug(f"MediaRecorder health check error: {e}")

                if was_cancelled:
                    logger.info("Recording cancelled by user request.")
                    await heavy_context.close()
                    await browser.close()
                    raise asyncio.CancelledError("Job cancelled during recording")

                if was_stuck:
                    logger.warning(
                        "MediaRecorder aborted — recording was stuck or blank; failing download."
                    )
                    await heavy_context.close()
                    await browser.close()
                    raise RuntimeError(
                        "Video failed to download: MediaRecorder output was stuck or blank."
                    )
                else:
                    logger.debug("Stopping MediaRecorder…")
                    try:
                        async with heavy_page.expect_download(timeout=15000) as dl_info:
                            await target_frame.evaluate('''async () => {
                                const downloadText = (name, text) => {
                                    const blob = new Blob([text], { type: "text/plain" });
                                    const a = document.createElement("a");
                                    a.href = URL.createObjectURL(blob);
                                    a.download = name;
                                    document.body.appendChild(a);
                                    a.click();
                                };

                                if (!window.mediaRecorder) {
                                    downloadText(
                                        "failed.txt",
                                        `missing-media-recorder:${window.mediaRecorderError || "unknown"}`
                                    );
                                    return;
                                }

                                const recorder = window.mediaRecorder;
                                window.recorderChunks = (window.recorderChunks || []).filter(
                                    chunk => chunk && chunk.size > 0
                                );

                                await new Promise((resolve) => {
                                    const finish = () => setTimeout(resolve, 250);
                                    recorder.addEventListener("stop", finish, { once: true });
                                    try {
                                        if (recorder.state === "recording") recorder.requestData();
                                    } catch (_) {}
                                    try {
                                        if (recorder.state !== "inactive") recorder.stop();
                                        else finish();
                                    } catch (_) {
                                        finish();
                                    }
                                });

                                window.recorderChunks = (window.recorderChunks || []).filter(
                                    chunk => chunk && chunk.size > 0
                                );
                                const totalSize = window.recorderChunks.reduce((sum, chunk) => sum + chunk.size, 0);
                                if (totalSize <= 0) {
                                    const stats = JSON.stringify(window.mediaRecorderStats || {});
                                    downloadText(
                                        "failed.txt",
                                        `empty-media-recorder:${totalSize}:` +
                                        `${window.mediaRecorderError || "no-recorder-error"}:${stats}`
                                    );
                                    return;
                                }

                                const blob = new Blob(window.recorderChunks, { type: "video/webm" });
                                const a = document.createElement("a");
                                a.href = URL.createObjectURL(blob);
                                a.download = "recording.webm";
                                document.body.appendChild(a);
                                a.click();
                            }''')
                        download = await dl_info.value
                        if "recording.webm" in download.suggested_filename:
                            filename = f"{uuid.uuid4().hex}.webm"
                            vid_path = os.path.join(storage, filename)
                            await download.save_as(vid_path)
                            saved_size = os.path.getsize(vid_path) if os.path.exists(vid_path) else 0
                            logger.info(
                                f"[MediaRecorder] Saved WebM recording: "
                                f"{os.path.basename(vid_path)} ({saved_size // 1024}KB)."
                            )
                            mp4_path = _convert_to_mp4(vid_path)
                            if not mp4_path:
                                raise RuntimeError(
                                    "MediaRecorder output was empty or corrupt; recording failed."
                                )
                            final_filename = os.path.basename(mp4_path)
                            temp_video_url = f"{_settings.base_url}/temp_storage/{final_filename}"
                            logger.debug(f"Final recording URL: {temp_video_url}")
                        else:
                            failure_path = os.path.join(storage, f"{uuid.uuid4().hex}_{download.suggested_filename}")
                            failure_detail = download.suggested_filename
                            try:
                                await download.save_as(failure_path)
                                with open(failure_path, "r", encoding="utf-8", errors="replace") as failure_file:
                                    failure_detail = failure_file.read(1000).strip() or failure_detail
                            except Exception as read_err:
                                failure_detail = f"{failure_detail}; could not read failure detail: {read_err}"
                            finally:
                                try:
                                    if os.path.exists(failure_path):
                                        os.remove(failure_path)
                                except Exception:
                                    pass
                            logger.error(
                                f"MediaRecorder download failed: "
                                f"{failure_detail}"
                            )
                            raise RuntimeError(
                                f"MediaRecorder output was empty or unavailable: {failure_detail}"
                            )
                    except Exception as e:
                        logger.error(f"MediaRecorder Heavy Pass failed: {e}")
                        await heavy_context.close()
                        await browser.close()
                        raise

                await heavy_context.close()

            await browser.close()

    except asyncio.CancelledError:
        raise
    except Exception as e:
        raise Exception(f"Playwright pipeline failed: {e}")

    # Extract thumbnail from page HTML
    thumbnail_url = None
    if soup:
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
