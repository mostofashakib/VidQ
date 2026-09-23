import asyncio
import base64
import logging
import os
import random
import re

from app.config import get_settings
from app.services.prompts import Prompts
from app.services.scraper.html import _clean_for_interaction
from app.services.scraper.computer_use import ComputerUse
from app.services.scraper.playback_graph import (
    PlaybackGraph,
    PlaybackState,
    poll_until_playing,
)

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
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
]

# Stealth script injected before every page navigation.
# Patches the most common bot-detection fingerprints so the browser appears
# to be a real Chrome install rather than headless Playwright.
STEALTH_JS = r"""
(function() {
    // 1. navigator.webdriver → undefined (the most-checked bot signal)
    try {
        Object.defineProperty(navigator, 'webdriver', {get: () => undefined, configurable: true});
    } catch(e) {}

    // 2. window.chrome — real Chrome exposes this object
    if (!window.chrome) {
        window.chrome = {
            app: {
                isInstalled: false,
                InstallState: {DISABLED:'disabled', INSTALLED:'installed', NOT_INSTALLED:'not_installed'},
                RunningState: {CANNOT_RUN:'cannot_run', READY_TO_RUN:'ready_to_run', RUNNING:'running'}
            },
            runtime: {
                OnInstalledReason: {INSTALL:'install', UPDATE:'update', CHROME_UPDATE:'chrome_update', SHARED_MODULE_UPDATE:'shared_module_update'},
                OnRestartRequiredReason: {APP_UPDATE:'app_update', OS_UPDATE:'os_update', PERIODIC:'periodic'},
                PlatformArch: {ARM:'arm', X86_32:'x86-32', X86_64:'x86-64'},
                PlatformOs: {ANDROID:'android', CROS:'cros', LINUX:'linux', MAC:'mac', WIN:'win'},
                RequestUpdateCheckStatus: {NO_UPDATE:'no_update', THROTTLED:'throttled', UPDATE_AVAILABLE:'update_available'}
            }
        };
    }

    // 3. navigator.permissions — headless breaks notification permission queries
    try {
        const origQuery = window.navigator.permissions.query.bind(navigator.permissions);
        window.navigator.permissions.query = (params) =>
            params.name === 'notifications'
                ? Promise.resolve({state: Notification.permission, onchange: null})
                : origQuery(params);
    } catch(e) {}

    // 4. navigator.plugins — empty in headless, non-empty in real Chrome
    try {
        const fakePdf = {
            name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer',
            description: 'Portable Document Format', length: 0,
        };
        const fakePdfV = {
            name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai',
            description: '', length: 0,
        };
        const fakeNacl = {
            name: 'Native Client', filename: 'internal-nacl-plugin',
            description: '', length: 0,
        };
        Object.defineProperty(navigator, 'plugins', {
            get: () => {
                const arr = [fakePdf, fakePdfV, fakeNacl];
                Object.setPrototypeOf(arr, PluginArray.prototype);
                return arr;
            }, configurable: true
        });
    } catch(e) {}

    // 5. navigator.languages
    try {
        Object.defineProperty(navigator, 'languages', {get: () => ['en-US', 'en'], configurable: true});
    } catch(e) {}

    // 6. navigator.hardwareConcurrency — headless typically returns 2; real machines return 4–16
    try {
        Object.defineProperty(navigator, 'hardwareConcurrency', {get: () => 8, configurable: true});
    } catch(e) {}

    // 7. WebGL vendor / renderer — headless shows "Google SwiftShader" which is a bot signal
    try {
        const proto = WebGLRenderingContext.prototype;
        const origGet = proto.getParameter;
        proto.getParameter = function(p) {
            if (p === 37445) return 'Intel Inc.';      // UNMASKED_VENDOR_WEBGL
            if (p === 37446) return 'Intel Iris OpenGL Engine';  // UNMASKED_RENDERER_WEBGL
            return origGet.call(this, p);
        };
    } catch(e) {}

    // 8. Mime-type detection — bots often have no MIME types registered
    try {
        Object.defineProperty(navigator, 'mimeTypes', {
            get: () => {
                const arr = [
                    {type:'application/pdf', suffixes:'pdf', description:'Portable Document Format', enabledPlugin: null},
                ];
                Object.setPrototypeOf(arr, MimeTypeArray.prototype);
                return arr;
            }, configurable: true
        });
    } catch(e) {}
})();
"""


async def _inject_stealth(context) -> None:
    """
    Register STEALTH_JS as an init script on a Playwright browser context.
    It fires on every page/frame before any site JS runs, so fingerprint patches
    are in place before the site's bot-detection code can read them.
    """
    try:
        await context.add_init_script(STEALTH_JS)
    except Exception as e:
        logger.debug(f"Stealth script injection failed: {e}")


# ── Human behaviour helpers ───────────────────────────────────────────────────

async def _human_mouse_wander(page, n: int = 5) -> None:
    """Move the mouse to random positions to simulate natural human behaviour."""
    try:
        for _ in range(n):
            x = random.randint(160, 1760)
            y = random.randint(80, 920)
            await page.mouse.move(x, y)
            await asyncio.sleep(random.uniform(0.15, 0.55))
    except Exception:
        pass


async def _human_scroll(page, n: int = 2) -> None:
    """Scroll down briefly then return to top — mimics a human scanning the page."""
    try:
        for _ in range(n):
            await page.evaluate(f"window.scrollBy(0, {random.randint(150, 500)})")
            await asyncio.sleep(random.uniform(0.4, 1.0))
        await asyncio.sleep(random.uniform(0.2, 0.5))
        await page.evaluate("window.scrollTo(0, 0)")
    except Exception:
        pass


# ── Cloudflare challenge detection + bypass ───────────────────────────────────

# Page title substrings that indicate a Cloudflare interstitial
_CF_TITLE_TRIGGERS = (
    "just a moment",
    "security check",
    "attention required",
    "performing security verification",
    "checking your browser",
    "verifying you are human",
    "one more step",
    "please wait",
    "access denied",
    "ddos-guard",
)

# DOM selectors that confirm a CF challenge is present
_CF_DOM_CHECK_JS = """() => {
    const sel = [
        '#challenge-running',
        '#challenge-form',
        '#cf-challenge-running',
        '.cf-turnstile',
        '[data-sitekey]',
        'iframe[src*="challenges.cloudflare.com"]',
        'iframe[src*="turnstile"]',
        'iframe[src*="recaptcha"]',
    ];
    for (const s of sel) {
        if (document.querySelector(s)) return true;
    }
    const bodyText = (document.body && document.body.innerText) || '';
    return (
        bodyText.includes('Performing security verification') ||
        bodyText.includes('Verifying you are human') ||
        bodyText.includes('Checking your browser') ||
        bodyText.includes('Please wait while we check')
    );
}"""


async def _is_cloudflare_challenge(page) -> bool:
    """Return True if the page is showing a Cloudflare / bot-verification challenge."""
    try:
        title = (await page.title()).lower()
        if any(t in title for t in _CF_TITLE_TRIGGERS):
            return True
        return bool(await page.evaluate(_CF_DOM_CHECK_JS))
    except Exception:
        return False


async def _bypass_cloudflare(page, llm_manager=None, max_wait: int = 30) -> bool:
    """
    Attempt to pass a Cloudflare Turnstile or interstitial challenge.

    Strategy (in order):
    1. Wait a few seconds — good fingerprint/cookies often auto-clear the challenge.
    2. Human mouse wander before interaction.
    3. DOM bounding-rect of the CF iframe → click at the checkbox position
       (Turnstile checkbox sits ~24 px from the left edge, vertically centred).
    4. LLM screenshot → pixel coordinates (when llm_manager is available).
    5. Heuristic fallback clicks at common Turnstile positions.
    6. Poll for up to `max_wait` seconds for the challenge to clear.

    Returns True when the challenge page is no longer detected.
    """
    logger.info("Cloudflare challenge detected — attempting bypass…")
    cu = ComputerUse(page)

    # Step 1 — wait and see if real-Chrome fingerprint auto-clears it
    await asyncio.sleep(random.uniform(2.5, 4.5))
    if not await _is_cloudflare_challenge(page):
        logger.info("CF bypass: auto-cleared (fingerprint / cached cookies).")
        return True

    # Step 2 — natural mouse wander before touching anything
    await _human_mouse_wander(page, n=4)
    await asyncio.sleep(random.uniform(0.5, 1.2))

    clicked = False

    # Step 3 — DOM bounding rect: find the CF iframe and click at its checkbox
    try:
        rect = await page.evaluate("""() => {
            const candidates = [
                'iframe[src*="challenges.cloudflare.com"]',
                'iframe[src*="turnstile"]',
                '.cf-turnstile iframe',
                '#challenge-form iframe',
                '#cf-challenge-running iframe',
            ];
            for (const s of candidates) {
                const el = document.querySelector(s);
                if (el) {
                    const b = el.getBoundingClientRect();
                    if (b.width > 0 && b.height > 0)
                        return { x: b.left, y: b.top, w: b.width, h: b.height };
                }
            }
            // Fallback to container when iframe isn't directly accessible
            const container = document.querySelector('.cf-turnstile')
                || document.querySelector('#challenge-form')
                || document.querySelector('.challenge-container');
            if (container) {
                const b = container.getBoundingClientRect();
                if (b.width > 0 && b.height > 0)
                    return { x: b.left, y: b.top, w: b.width, h: b.height };
            }
            return null;
        }""")
        if rect:
            # Turnstile checkbox is ~24 px from the left edge, vertically centred
            cx = int(rect["x"]) + 24 + random.randint(-4, 4)
            cy = int(rect["y"]) + int(rect["h"] / 2) + random.randint(-3, 3)
            await asyncio.sleep(random.uniform(0.6, 1.4))
            await cu.click_at_pixel(cx, cy)
            clicked = True
            logger.info(f"CF bypass: Turnstile checkbox click at ({cx}, {cy})")
    except Exception as e:
        logger.debug(f"CF bypass DOM strategy failed: {e}")

    # Step 4 — LLM vision: take screenshot and ask LLM where to click
    if not clicked and llm_manager:
        try:
            shot_bytes = await cu.screenshot(quality=85)
            shot_b64 = base64.b64encode(shot_bytes).decode()
            result = await llm_manager.execute(Prompts.cloudflare_bypass(), shot_b64)
            px, py = result.get("pixel_x"), result.get("pixel_y")
            if px and py:
                await asyncio.sleep(random.uniform(0.6, 1.4))
                await cu.click_at_pixel(int(px), int(py))
                clicked = True
                logger.info(f"CF bypass: LLM-directed click at ({px}, {py})")
        except Exception as e:
            logger.debug(f"CF bypass LLM strategy failed: {e}")

    # Step 5 — heuristic positions (1920×1080 viewport)
    if not clicked:
        logger.debug("CF bypass: falling back to heuristic pixel positions.")
        heuristic = [
            (960, 540),   # page centre (compact centred widget)
            (960, 480),   # slightly above centre
            (747, 540),   # left-third (widget often left-aligned on wider pages)
            (80, 540),    # far-left (fully left-aligned compact widget)
        ]
        for hx, hy in heuristic:
            await asyncio.sleep(random.uniform(0.4, 0.9))
            await cu.click_at_pixel(
                hx + random.randint(-5, 5),
                hy + random.randint(-5, 5),
            )

    # Step 6 — poll for the challenge to clear
    logger.info("CF bypass: waiting for challenge page to clear…")
    for _ in range(max_wait // 2):
        await asyncio.sleep(2)
        if not await _is_cloudflare_challenge(page):
            logger.info("CF bypass: challenge cleared!")
            await asyncio.sleep(random.uniform(1.0, 2.0))  # let the real page settle
            return True

    logger.warning(f"CF bypass: challenge did not clear within {max_wait}s.")
    return False


_PLAYING_JS = '''() => {
    const vs = Array.from(document.querySelectorAll('video'));
    return vs.length > 0 && vs.some(v => !v.paused || v.currentTime > 0.5);
}'''

_IS_MEDIA_READY_JS = '''() => {
    function isLikelyMediaReady(container) {
        const video = container.querySelector("video");
        const playButton = container.querySelector(
            'button[aria-label*="Play"], button[title*="Play"], [role="button"][aria-label*="Play"]'
        );
        const loader = container.querySelector(
            '[class*="loading"], [class*="spinner"], [aria-busy="true"]'
        );
        const visiblePlay =
            playButton &&
            playButton.offsetParent !== null &&
            !playButton.disabled;
        const videoReady =
            video &&
            video.readyState >= 2 &&
            video.videoWidth > 0 &&
            video.videoHeight > 0;
        const noLoader = !loader || loader.offsetParent === null;
        return noLoader && (visiblePlay || videoReady);
    }
    return isLikelyMediaReady(document.body);
}'''


async def _wait_for_media_ready(page, timeout_s: int = 15) -> bool:
    """
    Poll the page AND all child frames up to `timeout_s` seconds.
    Returns True as soon as the video is playing or the player UI is ready.
    Prioritises _PLAYING_JS (video actively playing) over the readiness heuristic,
    and checks child frames so iframe-embedded players are detected.
    """
    for _ in range(timeout_s):
        try:
            if await page.evaluate(_PLAYING_JS):
                logger.info("Media already playing (autoplay detected).")
                return True
            if await page.evaluate(_IS_MEDIA_READY_JS):
                return True
            for frame in page.frames[1:]:
                try:
                    if await frame.evaluate(_PLAYING_JS):
                        logger.info("Media playing in iframe (autoplay detected).")
                        return True
                    if await frame.evaluate(_IS_MEDIA_READY_JS):
                        return True
                except Exception:
                    pass
        except Exception:
            pass
        await asyncio.sleep(1)
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
    Heuristic pre-pass: press Escape, then use the DOM accessibility tree to
    dismiss consent banners / age-gates, then fall back to pure JS for anything
    the ARIA pass missed (ad overlays, custom close buttons, countdown timers).

    Returns the number of elements successfully clicked.
    Runs at the start of every _agentic_interact attempt so the LLM always
    sees a cleaner page state.
    """
    cu = ComputerUse(page)
    await cu.press_key("Escape")
    await asyncio.sleep(0.1)

    # ARIA-first: catch consent buttons exposed via accessible names
    aria_clicked = await cu.find_and_click_consent()
    if aria_clicked:
        logger.info(f"Pre-pass ARIA: dismissed {aria_clicked} consent element(s).")
        await asyncio.sleep(0.3)

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

    total = aria_clicked + clicked
    if clicked:
        logger.info(f"Pre-pass JS: dismissed {clicked} overlay/banner element(s).")
        await asyncio.sleep(0.5)
    return total


async def _try_click(page, selector: str) -> bool:
    """
    Click `selector` via ComputerUse (native → JS → per-frame).
    Covers iframe-embedded players without the caller needing to know
    which frame holds the element.
    """
    return await ComputerUse(page).click_by_selector(selector)


async def _try_direct_play(page, max_click_retries: int = 3) -> bool:
    """
    Heuristic play-button trigger — no LLM.
    1. Accessibility tree: find_and_click_play() via ARIA roles (most reliable).
    2. CSS heuristics: common video-player selectors, main + child frames.
    3. <video> element direct click (last resort).
    """
    cu = ComputerUse(page)

    async def _check_after_click(label: str, retry_index: int) -> bool:
        if await poll_until_playing(page, _is_playing, timeout_s=1.2, interval_s=0.15):
            logger.info(
                f"[Agent] Playback confirmed after {label} click "
                f"(try {retry_index}/{max_click_retries})."
            )
            return True
        dismissed = await _pre_pass_unblock(page)
        if dismissed:
            logger.info(
                f"[Agent] Popup/overlay handled after {label} click "
                f"(try {retry_index}/{max_click_retries})."
            )
        else:
            logger.info(
                f"[Agent] {label} click did not start playback "
                f"(try {retry_index}/{max_click_retries})."
            )
        return False

    # ARIA-first: use the accessibility tree to locate the play button
    for retry_index in range(1, max_click_retries + 1):
        logger.info(f"[Agent] Trying accessibility play button click ({retry_index}/{max_click_retries}).")
        if await cu.find_and_click_play():
            if await _check_after_click("accessibility play", retry_index):
                return True
        else:
            logger.info("[Agent] Accessibility play button not found.")
            break

    # CSS heuristics fallback (covers players with missing ARIA labels)
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
    for target in [page, *page.frames[1:]]:
        for sel in selectors:
            try:
                el = await target.query_selector(sel)
                if el and await el.is_visible():
                    for retry_index in range(1, max_click_retries + 1):
                        logger.info(f"[Agent] Clicking heuristic play selector {sel!r} ({retry_index}/{max_click_retries}).")
                        await el.click()
                        if await _check_after_click(f"selector {sel!r}", retry_index):
                            return True
                        el = await target.query_selector(sel)
                        if not el or not await el.is_visible():
                            logger.info(f"[Agent] Selector {sel!r} no longer visible after retry.")
                            break
            except Exception:
                pass
        try:
            el = await target.query_selector('video')
            if el:
                for retry_index in range(1, max_click_retries + 1):
                    logger.info(f"[Agent] Clicking <video> element directly ({retry_index}/{max_click_retries}).")
                    await el.click()
                    if await _check_after_click("<video>", retry_index):
                        return True
                    el = await target.query_selector('video')
                    if not el:
                        logger.info("[Agent] <video> element disappeared after retry.")
                        break
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


async def _wait_for_playback_started(page, timeout_s: float = 5.0, interval_s: float = 0.5) -> bool:
    """Poll playback state before recorder injection."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while asyncio.get_running_loop().time() < deadline:
        if await _is_playing(page):
            logger.info("[Event] Playback-start detector: video is playing.")
            return True
        await asyncio.sleep(interval_s)
    logger.warning(f"[Event] Playback-start detector: no playback after {timeout_s:.1f}s.")
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


async def _media_session_play_and_fullscreen(page) -> bool:
    """Try Media Session API + fullscreen on the main video element."""
    try:
        success = await page.evaluate('''async () => {
            const vids = Array.from(document.querySelectorAll('video')).filter(v => v.readyState >= 1);
            if (!vids.length) return false;
            const main = vids.sort((a, b) => (b.offsetWidth * b.offsetHeight) - (a.offsetWidth * a.offsetHeight))[0];
            try { await main.play(); } catch (e) {}
            let played = false;
            try { played = !main.paused; } catch (e) {}
            try {
                if (!document.fullscreenElement && (main.requestFullscreen || main.webkitRequestFullscreen)) {
                    (main.requestFullscreen || main.webkitRequestFullscreen).call(main);
                }
            } catch (e) {}
            if (navigator.mediaSession) {
                try { navigator.mediaSession.playbackState = 'playing'; } catch (_) {}
            }
            return played;
        }''')
        if success:
            logger.info("MediaSession/Fullscreen kick succeeded.")
            await asyncio.sleep(1.0)
        return bool(success)
    except Exception as e:
        logger.debug(f"MediaSession fullscreen attempt failed: {e}")
        return False


def _log_strategy(reason: str) -> None:
    logger.info(f"[AgenticStrategy] {reason}")


async def _request_fullscreen_main_video(page) -> None:
    logger.info("[Agent] Requesting fullscreen for main video element.")
    try:
        result = await page.evaluate('''() => {
            const vids = Array.from(document.querySelectorAll('video')).filter(v => v.readyState >= 1);
            if (!vids.length) return { ok: false, reason: 'no-video' };
            const main = vids.sort((a, b) => (b.offsetWidth * b.offsetHeight) - (a.offsetWidth * a.offsetHeight))[0];
            if (!document.fullscreenElement && (main.requestFullscreen || main.webkitRequestFullscreen)) {
                (main.requestFullscreen || main.webkitRequestFullscreen).call(main);
                return { ok: true, reason: 'requested' };
            }
            return { ok: true, reason: document.fullscreenElement ? 'already-fullscreen' : 'unsupported' };
        }''')
        logger.info(f"[Agent] Fullscreen request result: {result}")
    except Exception as exc:
        logger.info(f"[Agent] Fullscreen request failed: {exc}")


async def _agentic_interact(page, llm_manager, max_attempts: int = 6) -> bool:
    """
    Agentic playback loop backed by PlaybackGraph (State Graph / FSM architecture).
    Coordinates pre-pass unblocking, fast heuristic triggers, LLM vision guidance,
    click-jacking popup recovery, and fullscreen confirmation with page-refresh resilience.
    """
    if not llm_manager:
        return False

    graph = PlaybackGraph(
        is_playing_fn=_is_playing,
        pre_pass_fn=_pre_pass_unblock,
        force_play_js_fn=_force_play_js,
        try_direct_play_fn=_try_direct_play,
        media_session_fn=_media_session_play_and_fullscreen,
        request_fullscreen_fn=_request_fullscreen_main_video,
    )
    state = PlaybackState(page=page, llm_manager=llm_manager, max_attempts=max_attempts)
    return await graph.run(state)


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
