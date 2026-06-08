"""
ComputerUse — centralized interface for all browser interaction activities.

Screenshots, button clicks, scrolling, and keyboard input all flow through
this class.  Element selection uses the DOM accessibility tree (Playwright
ARIA APIs: aria_snapshot + get_by_role) as the primary strategy, with CSS
selector fallback for non-standard players that don't expose proper ARIA.
"""
import logging

logger = logging.getLogger("ComputerUse")


class ComputerUse:
    """
    Wraps a Playwright Page and provides reliable browser interactions
    via the DOM accessibility tree, with CSS selector fallback.

    Usage:
        cu = ComputerUse(page)
        screenshot_bytes = await cu.screenshot()
        tree = await cu.aria_snapshot()
        await cu.click_by_role("button", name="Play")
        await cu.scroll("down", 300)
    """

    def __init__(self, page):
        self._page = page

    # ── Read operations ──────────────────────────────────────────────────────

    async def screenshot(self, quality: int = 80, full_page: bool = False) -> bytes:
        """Capture the current viewport (or full page) as JPEG bytes."""
        return await self._page.screenshot(
            type="jpeg", quality=quality, full_page=full_page
        )

    async def aria_snapshot(self) -> str:
        """
        Capture the DOM accessibility tree as a compact YAML-like string.
        Covers the main frame only.  Returns empty string on failure.
        """
        try:
            snap = await self._page.aria_snapshot()
            return snap or ""
        except Exception as e:
            logger.debug(f"aria_snapshot failed: {e}")
            return ""

    async def aria_snapshot_for_frame(self, frame) -> str:
        """Capture the accessibility snapshot for a specific child frame."""
        try:
            snap = await frame.aria_snapshot()
            return snap or ""
        except Exception as e:
            logger.debug(f"aria_snapshot (frame) failed: {e}")
            return ""

    # ── Click operations ──────────────────────────────────────────────────────

    async def click_by_role(
        self,
        role: str,
        name: str | None = None,
        exact: bool = False,
    ) -> bool:
        """
        Click an element by ARIA role and optional accessible name.
        Checks the main frame, then each child frame, in order.
        This is the most reliable click method — it survives CSS/class renames.
        """
        targets = [self._page, *self._page.frames[1:]]
        for target in targets:
            try:
                kwargs: dict = {}
                if name is not None:
                    kwargs["name"] = name
                    kwargs["exact"] = exact
                locator = target.get_by_role(role, **kwargs)
                count = await locator.count()
                if count > 0:
                    await locator.first.click(timeout=3000)
                    frame_label = "main" if target is self._page else target.url[:40]
                    logger.info(
                        f"ARIA click: role={role!r} name={name!r} ({frame_label})"
                    )
                    return True
            except Exception:
                pass
        return False

    async def click_by_selector(self, selector: str) -> bool:
        """
        Click by CSS selector.
        Strategy: native Playwright click → JS click → per-frame native → per-frame JS.
        """
        # 1. Native Playwright click (handles scrolling into view, visibility checks)
        try:
            await self._page.click(selector, timeout=2500, force=False)
            logger.info(f"Native click: {selector}")
            return True
        except Exception:
            pass

        # 2. JS click on main frame (bypasses pointer-events: none overlays)
        try:
            hit = await self._page.evaluate(
                f"() => {{ const el = document.querySelector({repr(selector)}); "
                f"if (el) {{ el.click(); return true; }} return false; }}"
            )
            if hit:
                logger.info(f"JS click: {selector}")
                return True
        except Exception:
            pass

        # 3. Per-frame native + JS click (iframe-embedded players)
        for frame in self._page.frames[1:]:
            try:
                el = await frame.query_selector(selector)
                if el and await el.is_visible():
                    await el.click(timeout=2500, force=False)
                    logger.info(f"Frame native click: {selector} ({frame.url[:40]})")
                    return True
            except Exception:
                pass
            try:
                hit = await frame.evaluate(
                    f"() => {{ const el = document.querySelector({repr(selector)}); "
                    f"if (el) {{ el.click(); return true; }} return false; }}"
                )
                if hit:
                    logger.info(f"Frame JS click: {selector} ({frame.url[:40]})")
                    return True
            except Exception:
                pass
        return False

    async def click(
        self,
        selector: str | None = None,
        role: str | None = None,
        name: str | None = None,
    ) -> bool:
        """
        Smart click: tries ARIA role first (when role/name are provided),
        then falls back to CSS selector.
        """
        if role:
            if await self.click_by_role(role, name=name):
                return True
        if selector:
            return await self.click_by_selector(selector)
        return False

    async def find_and_click_play(self) -> bool:
        """
        Locate and click a play button using the accessibility tree.
        Tries a set of exact ARIA names, then non-exact variants, then pixel
        coordinates (last resort for sandboxed iframes and canvas-based players).
        """
        exact_names = [
            "Play", "play",
            "Play video", "Play Video",
            "Play/Pause", "play/pause",
            "Start", "start",
            "Resume", "resume",
        ]
        for name in exact_names:
            if await self.click_by_role("button", name=name, exact=True):
                return True

        # Non-exact: catches "Play movie", "Play episode", "Play S01E01", etc.
        if await self.click_by_role("button", name="Play", exact=False):
            return True

        # Last resort: pixel-based click for players that expose no ARIA / are
        # inside sandboxed iframes where CSS/ARIA selectors cannot reach.
        if await self.find_play_by_pixel():
            return True

        return False

    async def find_and_click_consent(self) -> int:
        """
        Find and click consent / cookie-acceptance buttons using the
        accessibility tree.  Returns the number of elements clicked.

        Complements the JS-based _pre_pass_unblock; catches buttons that
        the DOM text scan misses (e.g. labelled via aria-label only).
        """
        accept_phrases = {
            "accept all", "accept cookies", "accept", "i accept",
            "agree to all", "agree", "got it", "ok", "allow all",
            "allow cookies", "consent", "i understand", "continue",
            "i am 18", "18+", "enter site",
        }
        clicked = 0
        targets = [self._page, *self._page.frames[1:]]
        for target in targets:
            try:
                buttons = target.get_by_role("button")
                count = await buttons.count()
                for i in range(min(count, 50)):
                    btn = buttons.nth(i)
                    try:
                        if not await btn.is_visible():
                            continue
                        aria_label = (await btn.get_attribute("aria-label") or "").strip().lower()
                        text = (await btn.inner_text() or "").strip().lower()
                        if any(phrase == text or phrase == aria_label for phrase in accept_phrases):
                            await btn.click(timeout=2000)
                            logger.info(f"ARIA consent click: '{text or aria_label}'")
                            clicked += 1
                    except Exception:
                        continue
            except Exception:
                pass
        return clicked

    async def click_at_pixel(self, x: int, y: int) -> bool:
        """
        Click at absolute viewport pixel coordinates.
        Sends a real synthesised mouse event — bypasses all DOM selection and
        works even when the player lives inside a sandboxed iframe or uses a
        canvas-based UI where ARIA and CSS selectors cannot reach.
        """
        try:
            await self._page.mouse.click(x, y)
            logger.info(f"Pixel click at ({x}, {y})")
            return True
        except Exception as e:
            logger.debug(f"pixel_click ({x}, {y}) failed: {e}")
            return False

    async def find_play_by_pixel(
        self,
        viewport_width: int = 1920,
        viewport_height: int = 1080,
    ) -> bool:
        """
        Click at the visual centre of the video player using pixel coordinates.

        Strategy (in order):
        1. Find the largest <video> element in the main frame and click at the
           centre of its bounding rect (getBoundingClientRect → viewport coords).
        2. Fall back to the largest visible <iframe> — the parent page can read
           its bounding rect even when the iframe body is cross-origin locked.
        3. Fall back to pre-defined heuristic positions in a 1920×1080 viewport
           (centre, slightly-above-centre, control-bar row, lower-third).
        """
        try:
            rect = await self._page.evaluate("""() => {
                // Largest video element in main frame
                const videos = Array.from(document.querySelectorAll('video'));
                if (videos.length) {
                    const v = videos.reduce((best, cur) => {
                        return (cur.offsetWidth * cur.offsetHeight) > (best.offsetWidth * best.offsetHeight)
                            ? cur : best;
                    }, videos[0]);
                    const b = v.getBoundingClientRect();
                    if (b.width > 0 && b.height > 0)
                        return { x: b.left + b.width / 2, y: b.top + b.height / 2, src: 'video' };
                }
                // Largest visible iframe (proxy for embedded player)
                const iframes = Array.from(document.querySelectorAll('iframe'))
                    .filter(f => { const b = f.getBoundingClientRect(); return b.width > 0 && b.height > 0; });
                if (iframes.length) {
                    const f = iframes.reduce((best, cur) => {
                        const ba = best.getBoundingClientRect(), ca = cur.getBoundingClientRect();
                        return (ca.width * ca.height) > (ba.width * ba.height) ? cur : best;
                    }, iframes[0]);
                    const b = f.getBoundingClientRect();
                    return { x: b.left + b.width / 2, y: b.top + b.height / 2, src: 'iframe' };
                }
                return null;
            }""")
            if rect and rect.get("x") and rect.get("y"):
                cx, cy = int(rect["x"]), int(rect["y"])
                logger.info(f"Pixel play: found {rect.get('src', '?')} centre at ({cx}, {cy})")
                return await self.click_at_pixel(cx, cy)
        except Exception as e:
            logger.debug(f"find_play_by_pixel JS lookup failed: {e}")

        # Heuristic fallback for when no element bounding rect is available
        heuristic_positions = [
            (viewport_width // 2, viewport_height // 2),
            (viewport_width // 2, viewport_height // 2 - 60),
            (viewport_width // 2, int(viewport_height * 0.88)),
            (viewport_width // 2, int(viewport_height * 0.75)),
        ]
        for x, y in heuristic_positions:
            if await self.click_at_pixel(x, y):
                return True
        return False

    # ── Input operations ──────────────────────────────────────────────────────

    async def scroll(self, direction: str = "down", pixels: int = 300) -> None:
        """Scroll the page in the given direction by `pixels` pixels."""
        delta = pixels if direction == "down" else -pixels
        try:
            await self._page.evaluate(f"window.scrollBy(0, {delta})")
        except Exception as e:
            logger.debug(f"scroll failed: {e}")

    async def press_key(self, key: str) -> None:
        """Send a keyboard key press (e.g. 'Escape', 'Space', 'Tab', 'Enter')."""
        try:
            await self._page.keyboard.press(key)
        except Exception as e:
            logger.debug(f"key press '{key}' failed: {e}")

    async def mouse_move(self, x: int, y: int) -> None:
        """Move the mouse cursor to the given page coordinates."""
        try:
            await self._page.mouse.move(x, y)
        except Exception as e:
            logger.debug(f"mouse_move ({x}, {y}) failed: {e}")
