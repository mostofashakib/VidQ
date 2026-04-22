import asyncio
import base64
import logging
import os
import re

from app.config import get_settings
from app.services.prompts import Prompts
from app.services.scraper.html import _clean_for_interaction

logger = logging.getLogger("VideoScraper")

_settings = get_settings()

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

_PLAYING_JS = '''() => {
    const vs = Array.from(document.querySelectorAll('video'));
    return vs.length > 0 && vs.some(v => !v.paused || v.currentTime > 0.5);
}'''


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


async def _agentic_interact(page, llm_manager, max_attempts: int = 6) -> bool:
    """
    Agentic playback loop.  Goal: get the MAIN video playing so it can be
    recorded.  Each iteration runs three layers in order:

      Layer 1 – No-LLM fast path
        a. JS pre-pass: dismiss visible consent / ad overlays.
        b. force_play_js: call video.play() directly.
        c. Heuristic selectors: click common play-button CSS selectors.

      Layer 2 – Vision + HTML LLM guidance
        Capture screenshot + interaction-safe HTML, pass both to the LLM.
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
            with open(os.path.join(_settings.temp_storage_dir, f"debug_agentic_{attempt}.jpg"), "wb") as f:
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
