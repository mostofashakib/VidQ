import os
import logging
import httpx
import uuid
import asyncio
import random
import base64
import math
import threading
from urllib.parse import urlparse, urljoin
from playwright.async_api import async_playwright
from bs4 import BeautifulSoup
import subprocess
import re
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
    "header", "footer", "nav", "aside", "form",
    "ads", "advertisement",
]
DROPPED_ATTRS = ["style", "onclick", "onload", "data-tracking"]


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(DROPPED_TAGS):
        tag.decompose()

    for link in soup.find_all("link"):
        if not link or not hasattr(link, "attrs") or link.attrs is None:
            continue
        rel_attr = link.get("rel") or []
        rel = [r.lower() for r in rel_attr] if isinstance(rel_attr, list) else [str(rel_attr).lower()]
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

    ignore_classes_ids = ["video__preview", "about-chips-list", "cookies-modal", "ad-detector", "asg-"]
    for pattern in ignore_classes_ids:
        for el in soup.find_all(class_=lambda x: x and pattern in x):
            el.decompose()
        for el in soup.find_all(id=lambda x: x and pattern in x):
            el.decompose()

    for tag in soup.find_all(True):
        for attr in DROPPED_ATTRS:
            tag.attrs.pop(attr, None)
        for attr in list(tag.attrs.keys()):
            if attr.startswith("data-") and attr not in ("data-src", "data-video-id", "data-duration"):
                del tag.attrs[attr]

    cleaned = str(soup)
    logger.debug(f"HTML cleaned: {len(html)} → {len(cleaned)} chars ({100*(1-len(cleaned)/max(len(html),1)):.0f}% reduction)")
    return cleaned


def _clean_for_interaction(html: str, max_len: int = 7000) -> str:
    """
    Interaction-focused HTML cleaner used by _agentic_interact.

    Crucially different from clean_html:
    - Does NOT remove cookie banners, consent dialogs, ad overlays — those are
      exactly the elements we need the LLM to see and click.
    - Strips only non-interactive noise (scripts, images, audio, canvas, svg).
    - Preserves class/id/role/aria attrs for CSS selector generation.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(["script", "style", "noscript", "img", "picture",
                               "audio", "canvas", "svg", "meta", "link", "head"]):
        tag.decompose()

    KEEP_ATTRS = {
        "class", "id", "role", "aria-label", "aria-hidden",
        "type", "name", "value", "href", "src", "data-src",
        "data-video-id", "tabindex", "title",
    }
    for tag in soup.find_all(True):
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in KEEP_ATTRS}

    text = str(soup)
    text = re.sub(r"\n\s*\n", "\n", text)
    text = re.sub(r"  +", " ", text)

    if len(text) > max_len:
        half = max_len // 2
        text = text[:half] + "\n...[truncated]...\n" + text[-half:]

    return text


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

_AD_DOMAINS = frozenset([
    'tsyndicate.com', 'svacdn.tsyndicate.com',
    'doubleclick.net', 'googlesyndication.com',
    'adnxs.com', 'advertising.com', 'adsrvr.org',
    'adcolony.com', 'inmobi.com', 'rubiconproject.com',
    'pubmatic.com', 'openx.net', 'triplelift.com',
    'moatads.com', 'scorecardresearch.com',
])
# Matches dimension-like substrings in URL paths: 440x250, 320x240, 160x90, etc.
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


async def _interruptible_sleep(seconds: float, cancel_event=None) -> bool:
    """
    Sleep for `seconds`. Returns True if a cancel_event was set before completion.
    Supports threading.Event and asyncio.Event.
    """
    end = asyncio.get_event_loop().time() + seconds
    while True:
        remaining = end - asyncio.get_event_loop().time()
        if remaining <= 0:
            return False
        if cancel_event is not None and cancel_event.is_set():
            return True
        await asyncio.sleep(min(1.0, remaining))


async def _get_main_video_selector(page, llm_selector: str | None = None) -> str:
    """Return a CSS selector for the largest (main) video element."""
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


async def _pre_pass_unblock(page) -> int:
    """
    Pure-JS heuristic pre-pass: dismiss visible consent banners, ad overlays,
    countdown timers, and age-gates WITHOUT involving the LLM.

    Returns the number of elements successfully clicked.
    Runs at the start of every _agentic_interact attempt so the LLM always
    sees a cleaner page state.
    """
    clicked = await page.evaluate(r"""() => {
        let count = 0;

        // ── 1. Cookie / consent banners ──────────────────────────────────
        const consentPhrases = [
            'accept all', 'accept cookies', 'i accept', 'accept', 'agree to all',
            'agree', 'got it', 'ok', 'allow all', 'allow cookies',
            'consent', 'i understand', 'understood',
        ];
        const consentContainerRe = /cookie|consent|gdpr|privacy|notice|banner|popup|modal|overlay/i;

        const allClickable = Array.from(document.querySelectorAll(
            'button, [role="button"], a, input[type="submit"], input[type="button"]'
        ));

        for (const el of allClickable) {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;

            const text = (
                el.textContent || el.value || el.getAttribute('aria-label') || ''
            ).trim().toLowerCase();

            const ownClass  = (el.className || '').toString();
            const parentClass = (el.closest('[class]') && el.closest('[class]').className) || '';
            const isInsideConsent = consentContainerRe.test(ownClass + ' ' + parentClass);

            if (isInsideConsent && consentPhrases.some(p => text === p || text.startsWith(p))) {
                el.click();
                count++;
            }
        }

        // ── 2. Ad overlay close / dismiss buttons ────────────────────────
        const adContainerRe = /\bad\b|advert|sponsor|promo|overlay|popup/i;
        const closeRe       = /close|dismiss|skip|✕|×/i;

        const closeEls = Array.from(document.querySelectorAll(
            '[class*="close"], [class*="dismiss"], [class*="skip"],'
            + '[aria-label*="close" i], [aria-label*="dismiss" i], [aria-label*="skip" i]'
        ));

        for (const el of closeEls) {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            const parent = el.closest('[class]');
            if (parent && adContainerRe.test(parent.className)) {
                el.click();
                count++;
            }
        }

        // ── 3. Visible skip-ad / countdown skip buttons ──────────────────
        const skipEls = Array.from(document.querySelectorAll(
            '[class*="skip-ad"], [class*="skipAd"], [class*="skip_ad"],'
            + '[id*="skip-ad"], [id*="skipAd"]'
        ));
        for (const el of skipEls) {
            const rect = el.getBoundingClientRect();
            if (rect.width > 0 && rect.height > 0) {
                el.click();
                count++;
            }
        }

        // ── 4. Age-gate / login wall "continue" buttons ──────────────────
        const agePhrases = ['i am 18', '18+', 'enter site', 'continue', 'i confirm'];
        const ageContainerRe = /age.?gate|age.?verif|adult|18|mature/i;
        for (const el of allClickable) {
            const rect = el.getBoundingClientRect();
            if (rect.width === 0 || rect.height === 0) continue;
            const text = (el.textContent || '').trim().toLowerCase();
            const ownClass = (el.className || '').toString();
            const parentClass = (el.closest('[class]') && el.closest('[class]').className) || '';
            if (ageContainerRe.test(ownClass + ' ' + parentClass)
                && agePhrases.some(p => text.includes(p))) {
                el.click();
                count++;
            }
        }

        return count;
    }""")

    if clicked:
        logger.info(f"Pre-pass: dismissed {clicked} overlay/banner element(s).")
        await asyncio.sleep(1.2)
    return clicked


async def _try_click(page, selector: str) -> bool:
    """
    Try a Playwright-native click first (dispatches real pointer events),
    then fall back to JS click.
    """
    try:
        await page.click(selector, timeout=2500, force=False)
        logger.info(f"Playwright native click: {selector}")
        return True
    except Exception:
        pass
    try:
        hit = await page.evaluate(
            f"() => {{ const el = document.querySelector({repr(selector)}); if (el) {{ el.click(); return true; }} return false; }}"
        )
        if hit:
            logger.info(f"JS fallback click: {selector}")
        return bool(hit)
    except Exception:
        return False


async def _try_direct_play(page) -> bool:
    """
    Heuristic play-button trigger — no LLM. Tries common video player selectors
    and falls back to clicking the <video> element itself.
    """
    selectors = [
        '.vjs-big-play-button',
        '.jw-display-icon-container',
        '[data-plyr="play"]',
        '[aria-label*="Play" i]',
        '[title*="Play" i]',
        '[class*="big-play"]',
        '[class*="bigPlay"]',
        '[class*="play-btn"]',
        '[class*="PlayButton"]',
        '.player-play-btn',
        '#play-btn',
        '.fp-play',
        '.mejs__play',
        '.video-js .vjs-play-control',
    ]
    for sel in selectors:
        try:
            el = await page.query_selector(sel)
            if el and await el.is_visible():
                await el.click()
                await asyncio.sleep(1.5)
                if await _is_playing(page):
                    logger.info(f"Direct play via '{sel}'.")
                    return True
        except Exception:
            pass
    try:
        el = await page.query_selector('video')
        if el:
            await el.click()
            await asyncio.sleep(1.0)
            if await _is_playing(page):
                logger.info("Direct play via <video> click.")
                return True
    except Exception:
        pass
    return False


_PLAYING_JS = '''() => {
    const vs = Array.from(document.querySelectorAll('video'));
    return vs.length > 0 && vs.some(v => !v.paused || v.currentTime > 0.5);
}'''


async def _is_playing(page) -> bool:
    """Check main frame AND all child frames (handles iframe-embedded players)."""
    try:
        if await page.evaluate(_PLAYING_JS):
            return True
    except Exception:
        pass
    for frame in page.frames[1:]:
        try:
            if await frame.evaluate(_PLAYING_JS):
                return True
        except Exception:
            pass
    return False


async def _force_play_js(page) -> bool:
    """
    Directly call video.play() on every ready <video> element in the page and
    all child frames.  This is the most reliable trigger in headless Chromium
    because autoplay restrictions are lifted by default.
    Returns True if at least one video started playing.
    """
    _play_js = '''async () => {
        const vs = Array.from(document.querySelectorAll('video'));
        if (!vs.length) return false;
        let ok = false;
        for (const v of vs) {
            try {
                if (v.readyState >= 2) { await v.play(); ok = true; }
            } catch(e) {}
        }
        return ok;
    }'''
    _check_js = '''() => {
        const v = document.querySelector('video');
        return v ? !v.paused : false;
    }'''
    for target in [page, *page.frames[1:]]:
        try:
            started = await target.evaluate(_play_js)
            if started:
                await asyncio.sleep(1.5)
                if await target.evaluate(_check_js):
                    logger.info("Force JS video.play() succeeded.")
                    return True
        except Exception:
            pass
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
        _storage = os.path.join(os.path.dirname(__file__), '..', '..', 'temp_storage')
        os.makedirs(_storage, exist_ok=True)
        filename = f"{uuid.uuid4().hex}.mp4"
        out_path = os.path.join(_storage, filename)
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
            return f"http://localhost:8000/temp_storage/{filename}"
        err_tail = (stderr or b'').decode(errors='replace')[-300:]
        logger.warning(f"ffmpeg download failed (rc={proc.returncode}): {err_tail}")
    except Exception as e:
        logger.warning(f"ffmpeg download error: {e}")
    return None


async def _agentic_interact(page, llm_manager, max_attempts: int = 6) -> bool:
    """
    Agentic playback loop.  Goal: get the MAIN video playing so it can be
    recorded.  Each iteration runs three layers in order:

      Layer 1 – No-LLM fast path
        a. JS pre-pass: dismiss visible consent / ad overlays.
        b. force_play_js: call video.play() directly (works once the video
           element is loaded; skipped silently if the element doesn't exist
           yet or readyState < 2).
        c. Heuristic selectors: click common play-button CSS selectors.

      Layer 2 – Vision + HTML LLM guidance
        Capture screenshot + interaction-safe HTML, pass both to the LLM
        (vision model if available; Ollama degrades to text-only).
        LLM returns a single CSS selector to click RIGHT NOW.

      Layer 3 – Post-click recovery
        After the LLM click, if still not playing:
        - For unblocking clicks (consent/ad/age): wait extra, then run
          force_play_js + heuristic selectors.
        - For all clicks: run force_play_js.
    """
    if not llm_manager:
        return False

    for attempt in range(max_attempts):
        logger.info(f"--- Agentic attempt {attempt + 1}/{max_attempts} ---")

        # ── Layer 1a: JS pre-pass (no LLM) ──────────────────────────────
        dismissed = await _pre_pass_unblock(page)
        if dismissed:
            await asyncio.sleep(2.0)
            if await _is_playing(page):
                logger.info("  Playback after pre-pass dismissal.")
                return True

        # ── Layer 1b: force video.play() — works once element is loaded ──
        if await _force_play_js(page):
            return True

        # ── Layer 1c: heuristic play-button selectors ────────────────────
        if await _try_direct_play(page):
            return True

        # ── Layer 2: screenshot + HTML → LLM ────────────────────────────
        try:
            screenshot_bytes = await page.screenshot(type="jpeg", quality=80)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
        except Exception as e:
            logger.warning(f"  Screenshot failed: {e}")
            break

        try:
            with open(os.path.join(temp_storage_dir, f"debug_agentic_{attempt}.jpg"), "wb") as f:
                f.write(screenshot_bytes)
        except Exception:
            pass

        try:
            raw_html = await page.content()
            interact_html = _clean_for_interaction(raw_html, max_len=10000)
            logger.info(f"  HTML for LLM: {len(interact_html)} chars")
        except Exception as e:
            logger.warning(f"  HTML capture failed: {e}")
            break

        try:
            # Use vision call — Anthropic/OpenAI see the screenshot; Ollama
            # degrades gracefully to text-only via its fallback.
            result = await llm_manager.execute(
                Prompts.agentic_interact(interact_html, attempt),
                screenshot_b64,
            )
            selector = result.get("action_selector")
            reason = result.get("reason", "—")
            logger.info(f"  LLM action: '{selector}' — {reason}")
        except Exception as e:
            logger.warning(f"  LLM call failed: {e}")
            break

        if not selector:
            logger.info("  LLM returned null selector — nothing to click.")
            break

        # ── Layer 2 click ────────────────────────────────────────────────
        await _try_click(page, selector)

        # Unblocking clicks (consent / ad / age-gate) need extra settle time
        reason_ctx = (reason + " " + (selector or "")).lower()
        is_unblocking = any(kw in reason_ctx for kw in [
            "cookie", "consent", "accept", "banner", "age", "gdpr",
            "overlay", "modal", "popup", "close", "dismiss",
        ])
        await asyncio.sleep(3.5 if is_unblocking else 2.0)

        if await _is_playing(page):
            logger.info(f"  Playback confirmed after attempt {attempt + 1}.")
            return True

        # ── Layer 3: post-click recovery ─────────────────────────────────
        if await _force_play_js(page):
            return True

        if is_unblocking:
            logger.info("  Unblocking click — trying heuristic selectors.")
            if await _try_direct_play(page):
                return True

        logger.info(f"  Still not playing after attempt {attempt + 1}.")

    logger.warning("Agentic interact: all attempts exhausted without confirmed playback.")
    return False


async def _set_quality(page, settings_selector: str | None, quality_selector: str | None) -> None:
    """Optional: attempt to set video quality via settings menu."""
    try:
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

        await page.evaluate('''() => {
            const items = Array.from(document.querySelectorAll('.ytp-menuitem, [role="menuitem"]'));
            const qItem = items.find(i => {
                const txt = i.textContent.toLowerCase();
                return txt.includes('quality') || txt.includes('resolution') || txt.includes('1080p') || txt.includes('720p');
            });
            if (qItem) qItem.click();
        }''')
        await asyncio.sleep(0.6)

        await page.evaluate('''() => {
            const options = Array.from(document.querySelectorAll('.ytp-menuitem span, .vjs-menu-item, [role="menuitemradio"]'));
            const targets = ['720p', '1080p', '1440p', 'High'];
            for (const t of targets) {
                const found = options.find(o => o.textContent.includes(t));
                if (found) { found.click(); return true; }
            }
            if (options.length > 0) options[0].click();
        }''')
        logger.debug("Quality adjustment finished.")
    except Exception as e:
        logger.debug(f"Quality adjustment (optional) failed: {e}")


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


async def _is_forbidden(url: str, user_agent: str) -> bool:
    if not url or url.startswith("blob:"):
        return True
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

    _storage = os.path.join(os.path.dirname(__file__), '..', '..', 'temp_storage')
    os.makedirs(_storage, exist_ok=True)

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
                    with open(os.path.join(_storage, "debug_nav.jpg"), "wb") as f:
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
                # Heuristic fallback when no LLM
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
                    // Sort by visual area — largest is the main video
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
                with open(os.path.join(_storage, "debug_main.jpg"), "wb") as f:
                    f.write(screenshot_bytes)
            except Exception:
                pass

            # ── Identify main video URL and try direct download ──
            raw_urls = list(video_urls)
            # Belt-and-suspenders: filter any ad URLs that slipped through
            clean_urls = [u for u in raw_urls if not _is_ad_video_url(u)]
            logger.info(f"Video URLs: {len(raw_urls)} raw → {len(clean_urls)} after ad filter")

            # Priority: DOM currentSrc of the largest video first, then network-intercepted
            if main_dom_url and main_dom_url in clean_urls:
                ordered = [main_dom_url] + [u for u in clean_urls if u != main_dom_url]
            elif main_dom_url:
                ordered = [main_dom_url] + clean_urls
            else:
                ordered = clean_urls

            # Find the first accessible URL
            best_url: str | None = None
            for cand_url in ordered[:5]:
                if await _is_forbidden(cand_url, user_agent):
                    logger.warning(f"Forbidden URL (skipped): {cand_url[:80]}")
                    continue
                best_url = cand_url
                logger.info(f"Best video URL: {best_url[:100]}")
                break

            if best_url:
                # Try ffmpeg direct download — avoids expiring CDN session tokens on the frontend
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
                # Check for cancellation before starting expensive recording
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

                # Agentic playback (full loop with fresh screenshots each attempt)
                playing = await _agentic_interact(heavy_page, llm_manager, max_attempts=5)
                if not playing:
                    # Final attempt: direct JS .play() before giving up
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

                # Inject MediaRecorder — also calls .play() so the stream is live
                logger.info("Injecting MediaRecorder…")
                await heavy_page.evaluate(f'''async () => {{
                    window.recorderChunks = [];
                    const video = document.querySelector({repr(main_selector)});
                    if (video) {{
                        try {{
                            // Ensure video is playing before we capture the stream
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
                        vid_path = os.path.join(_storage, filename)
                        await download.save_as(vid_path)
                        mp4_path = _convert_to_mp4(vid_path)
                        final_filename = os.path.basename(mp4_path)
                        temp_video_url = f"http://localhost:8000/temp_storage/{final_filename}"
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
