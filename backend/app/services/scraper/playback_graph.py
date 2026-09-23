"""
PlaybackGraph — State Graph / Finite State Machine (FSM) for ComputerUse Playback.

Replaces the monolithic procedural retry loop with an explicit, typed state graph.
Encapsulates:
  - Event listener lifecycle (dialogs, popups, frame navigation)
  - Smart polling (zero-latency exits when playback starts vs fixed sleeps)
  - Explicit nodes: Unblock -> FastHeuristics -> Inspect -> LLMVision -> Execute -> PopupRecovery -> Fullscreen
  - Strategy caching & fault recovery
"""
from __future__ import annotations

import asyncio
import base64
import logging
from dataclasses import dataclass, field
from typing import Any, Callable

from app.config import get_settings
from app.services.prompts import Prompts
from app.services.scraper.computer_use import ComputerUse
from app.services.scraper.html import _clean_for_interaction

logger = logging.getLogger("VideoScraper.PlaybackGraph")
_settings = get_settings()


# ── Smart Polling Helpers ───────────────────────────────────────────────────

async def poll_until_playing(
    page: Any,
    is_playing_fn: Callable[[Any], Any],
    timeout_s: float = 1.5,
    interval_s: float = 0.15,
) -> bool:
    """
    Polls `is_playing_fn(page)` every `interval_s` up to `timeout_s`.
    Returns True immediately when playback is detected, eliminating
    wasteful fixed sleep delays.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        try:
            if await is_playing_fn(page):
                return True
        except Exception:
            pass
        await asyncio.sleep(interval_s)
    # Final check
    try:
        return bool(await is_playing_fn(page))
    except Exception:
        return False


# ── State Definition ────────────────────────────────────────────────────────

@dataclass
class PlaybackState:
    """Typed execution context passed across nodes in the playback state graph."""
    page: Any
    llm_manager: Any | None
    max_attempts: int = 6
    attempt: int = 0

    # Runtime flags
    is_playing: bool = False
    action_triggered: bool = False
    popup_seen: bool = False
    nav_after_action: bool = False
    playing_started: bool = False

    # Cached successful strategy
    preferred_strategy: str | None = None
    preferred_payload: dict[str, Any] = field(default_factory=dict)
    preferred_reason: str = ""
    preferred_failures: int = 0

    # Last executed action
    last_selector: str | None = None
    last_pixel: tuple[int, int] | None = None
    last_action_reason: str = ""
    is_unblocking_action: bool = False

    # Diagnostics
    history: list[str] = field(default_factory=list)

    def record_transition(self, node_name: str, message: str = "") -> None:
        entry = f"[{node_name}] {message}".strip()
        self.history.append(entry)
        logger.info(f"[PlaybackGraph] {entry}")

    def remember_strategy(self, name: str, reason: str, payload: dict | None = None) -> None:
        self.preferred_strategy = name
        self.preferred_reason = reason
        self.preferred_payload = payload or {}
        self.preferred_failures = 0
        logger.info(f"[PlaybackGraph] Cached successful playback strategy: {name} ({reason})")

    def invalidate_strategy(self) -> None:
        if self.preferred_strategy:
            logger.info(
                f"[PlaybackGraph] Clearing stale cached strategy {self.preferred_strategy}; "
                "falling back to full strategy stack."
            )
        self.preferred_strategy = None
        self.preferred_payload = {}
        self.preferred_reason = ""
        self.preferred_failures = 0


# ── Event Listener Scope (Context Manager) ──────────────────────────────────

class PlaybackListenerScope:
    """
    Context manager that safely binds browser dialog, popup, and navigation
    listeners to the PlaybackState and ensures 100% leak-free cleanup.
    """
    def __init__(self, state: PlaybackState):
        self.state = state
        self._on_dialog_handler: Any = None
        self._on_popup_handler: Any = None
        self._on_nav_handler: Any = None

    async def __aenter__(self) -> PlaybackListenerScope:
        page = self.state.page

        def _on_dialog(dialog: Any) -> None:
            logger.info(f"[PlaybackGraph] Auto-dismissing browser dialog: {dialog.type} — '{dialog.message[:60]}'")
            asyncio.ensure_future(dialog.dismiss())

        def _on_popup(new_page: Any) -> None:
            self.state.popup_seen = True
            popup_url = getattr(new_page, "url", "") or ""
            logger.info(f"[PlaybackGraph] Detected & closing popup window: {popup_url[:80]}")
            asyncio.ensure_future(new_page.close())

        def _on_nav(frame: Any) -> None:
            if frame == page.main_frame and self.state.action_triggered:
                self.state.nav_after_action = True
                logger.info(f"[PlaybackGraph] Page navigation/refresh detected: {getattr(frame, 'url', '')[:80]}")

        self._on_dialog_handler = _on_dialog
        self._on_popup_handler = _on_popup
        self._on_nav_handler = _on_nav

        page.on("dialog", _on_dialog)
        page.context.on("page", _on_popup)
        page.on("framenavigated", _on_nav)
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        page = self.state.page
        try:
            if self._on_dialog_handler:
                page.remove_listener("dialog", self._on_dialog_handler)
        except Exception:
            pass
        try:
            if self._on_popup_handler:
                page.context.remove_listener("page", self._on_popup_handler)
        except Exception:
            pass
        try:
            if self._on_nav_handler:
                page.remove_listener("framenavigated", self._on_nav_handler)
        except Exception:
            pass


# ── Playback Graph Implementation ───────────────────────────────────────────

class PlaybackGraph:
    """
    State Graph coordinating playback automation.
    Nodes:
      - node_detect_autoplay
      - node_cached_strategy
      - node_pre_pass_unblock
      - node_fast_heuristics
      - node_inspect_page
      - node_llm_action
      - node_execute_action
      - node_popup_recovery
      - node_post_click_recovery
      - node_confirm_fullscreen
    """

    def __init__(
        self,
        is_playing_fn: Callable[[Any], Any],
        pre_pass_fn: Callable[[Any], Any],
        force_play_js_fn: Callable[[Any], Any],
        try_direct_play_fn: Callable[[Any, int], Any],
        media_session_fn: Callable[[Any], Any],
        request_fullscreen_fn: Callable[[Any], Any],
    ):
        self.is_playing_fn = is_playing_fn
        self.pre_pass_fn = pre_pass_fn
        self.force_play_js_fn = force_play_js_fn
        self.try_direct_play_fn = try_direct_play_fn
        self.media_session_fn = media_session_fn
        self.request_fullscreen_fn = request_fullscreen_fn

    async def run(self, state: PlaybackState) -> bool:
        """Executes the playback state graph until playback is confirmed or attempts exhausted."""
        if not state.llm_manager:
            return False

        async with PlaybackListenerScope(state):
            # Node 0: Initial Autoplay check
            if await self.node_detect_autoplay(state):
                return True

            # Node 0.5: MediaSession fast kick
            state.action_triggered = True
            if await self.media_session_fn(state.page):
                state.remember_strategy("media_session", "MediaSession pre-kick")
                if await self.node_confirm_fullscreen(state, "MediaSession pre-kick"):
                    return True

            # Main Agentic Cycle Loop (Attempts)
            for attempt in range(state.max_attempts):
                state.attempt = attempt + 1
                state.popup_seen = False
                state.nav_after_action = False
                state.playing_started = False
                state.action_triggered = False

                state.record_transition("AttemptStart", f"Cycle {state.attempt}/{state.max_attempts}")

                # Check if already playing at attempt entry
                if await self.is_playing_fn(state.page):
                    state.record_transition("AutoplayDetected", f"Playing at attempt {state.attempt} entry")
                    if await self.node_confirm_fullscreen(state, f"attempt {state.attempt} entry"):
                        return True

                # Step 1: Cached Strategy Replay (if available)
                if state.preferred_strategy:
                    if await self.node_cached_strategy(state):
                        if await self.node_confirm_fullscreen(state, f"cached {state.preferred_strategy} replay"):
                            return True
                    state.preferred_failures += 1
                    if state.preferred_failures >= 2:
                        state.invalidate_strategy()
                    else:
                        continue

                # Step 2: Unblock Layer (ARIA + JS consent/ad pre-pass)
                if await self.node_pre_pass_unblock(state):
                    if await self.node_confirm_fullscreen(state, "pre-pass unblock"):
                        return True

                # Step 3: Fast Heuristics Layer (Force Play JS & Direct Play)
                if await self.node_fast_heuristics(state):
                    if await self.node_confirm_fullscreen(state, "fast heuristics"):
                        return True

                # Step 4: Page Inspection (Screenshot + ARIA + Clean HTML)
                inspection_data = await self.node_inspect_page(state)
                if not inspection_data:
                    state.record_transition("InspectFailed", "Could not capture inspection data")
                    break

                # Step 5: LLM Action Planning
                plan = await self.node_llm_action(state, inspection_data)
                if not plan or (not plan.get("selector") and plan.get("pixel_x") is None):
                    state.record_transition("NoActionPlanned", "LLM returned no selector or coordinates")
                    break

                # Step 6: Action Execution
                clicked = await self.node_execute_action(state, plan)

                # Step 7: Reactive Popup Recovery
                if clicked:
                    await self.node_popup_recovery(state, plan)
                    if await self.node_confirm_fullscreen(state, f"attempt {state.attempt} clicks"):
                        return True

                # Step 8: Post-click Recovery
                if await self.node_post_click_recovery(state):
                    return True

        logger.warning("[PlaybackGraph] All attempts exhausted without confirmed playback.")
        return False

    # ── Node Implementations ────────────────────────────────────────────────

    async def node_detect_autoplay(self, state: PlaybackState) -> bool:
        """Fast check: is the media element already active/autoplayed?"""
        if await self.is_playing_fn(state.page):
            state.record_transition("DetectAutoplay", "Video already playing via autoplay")
            return await self.node_confirm_fullscreen(state, "autoplay")
        return False

    async def node_cached_strategy(self, state: PlaybackState) -> bool:
        """Replay a previously successful strategy for this session."""
        strategy = state.preferred_strategy
        state.record_transition("ReplayStrategy", f"Replaying cached strategy: {strategy}")
        state.action_triggered = True

        if strategy == "media_session":
            return bool(await self.media_session_fn(state.page))
        if strategy == "pre_pass_unblock":
            dismissed = await self.pre_pass_fn(state.page)
            if dismissed:
                await poll_until_playing(state.page, self.is_playing_fn, timeout_s=1.5)
            return bool(await self.is_playing_fn(state.page))
        if strategy == "force_play_js":
            return bool(await self.force_play_js_fn(state.page))
        if strategy == "direct_play":
            return bool(await self.try_direct_play_fn(state.page, max_click_retries=3))

        if strategy in {"llm_selector", "llm_pixel", "heuristic_pixel"}:
            return await self._replay_click_payload(state, strategy)

        return False

    async def _replay_click_payload(self, state: PlaybackState, strategy: str) -> bool:
        cu = ComputerUse(state.page)
        payload = state.preferred_payload
        for retry_index in range(1, 4):
            clicked = False
            if strategy == "llm_selector" and payload.get("selector"):
                clicked = await cu.click_by_selector(payload["selector"])
            elif strategy == "llm_pixel" and payload.get("pixel_x") is not None:
                clicked = await cu.click_at_pixel(int(payload["pixel_x"]), int(payload["pixel_y"]))
                if clicked:
                    await self.request_fullscreen_fn(state.page)
            elif strategy == "heuristic_pixel":
                clicked = await cu.find_play_by_pixel()
                if clicked:
                    await self.request_fullscreen_fn(state.page)

            if not clicked:
                return False

            if await poll_until_playing(state.page, self.is_playing_fn, timeout_s=1.5):
                state.record_transition("ReplaySuccess", f"Cached {strategy} succeeded on retry {retry_index}")
                return True

            dismissed = await self.pre_pass_fn(state.page)
            saw_popup = state.popup_seen
            state.popup_seen = False
            if dismissed or saw_popup:
                logger.info(f"[PlaybackGraph] Handled overlay during cached {strategy} replay ({retry_index}/3)")

        return False

    async def node_pre_pass_unblock(self, state: PlaybackState) -> bool:
        """Pre-pass dismissal of consent banners, age-gates, and overlays."""
        state.record_transition("PrePassUnblock", "Running pre-pass unblock")
        state.action_triggered = True
        dismissed = await self.pre_pass_fn(state.page)
        if dismissed:
            # Smart polling: wait up to 1.5s for video playback or DOM settling
            if await poll_until_playing(state.page, self.is_playing_fn, timeout_s=1.5):
                state.remember_strategy("pre_pass_unblock", "pre-pass unblock")
                return True
        return False

    async def node_fast_heuristics(self, state: PlaybackState) -> bool:
        """Run non-LLM fast triggers: force video.play() and direct accessibility/CSS clicks."""
        state.record_transition("FastHeuristics", "Checking force_play_js and direct_play")
        state.action_triggered = True

        if await self.force_play_js_fn(state.page):
            state.remember_strategy("force_play_js", "force video.play()")
            return True

        if await self.try_direct_play_fn(state.page, max_click_retries=3):
            state.remember_strategy("direct_play", "accessibility/direct play")
            return True

        return False

    async def node_inspect_page(self, state: PlaybackState) -> dict[str, Any] | None:
        """Capture screenshot, ARIA snapshot, and interaction-safe HTML."""
        state.record_transition("InspectPage", "Capturing screenshot and ARIA tree")
        page = state.page
        cu = ComputerUse(page)

        vp = page.viewport_size or {"width": 1920, "height": 1080}
        vp_w, vp_h = vp["width"], vp["height"]

        try:
            screenshot_bytes = await cu.screenshot(quality=80)
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode()
        except Exception as e:
            logger.warning(f"[PlaybackGraph] Screenshot failed: {e}")
            return None

        # Capture compact ARIA tree
        aria_tree = await cu.aria_snapshot()
        for frame in page.frames[1:]:
            try:
                frame_snap = await cu.aria_snapshot_for_frame(frame)
                if frame_snap:
                    aria_tree += f"\n# iframe ({getattr(frame, 'url', '')[:60]})\n{frame_snap}"
            except Exception:
                pass

        # Capture HTML (lazy traversal limited to top frames to prevent memory bloat)
        try:
            raw_html = await page.content()
            for frame in page.frames[1:]:
                try:
                    f_html = await frame.content()
                    if f_html and len(f_html) > 500:
                        raw_html += f"\n<!-- IFRAME ({getattr(frame, 'url', '')[:80]}) -->\n" + f_html
                except Exception:
                    pass
            interact_html = _clean_for_interaction(raw_html, max_len=10000)
        except Exception as e:
            logger.warning(f"[PlaybackGraph] HTML capture error: {e}")
            interact_html = ""

        return {
            "screenshot_b64": screenshot_b64,
            "aria_tree": aria_tree,
            "interact_html": interact_html,
            "vp_w": vp_w,
            "vp_h": vp_h,
        }

    async def node_llm_action(self, state: PlaybackState, inspection: dict[str, Any]) -> dict[str, Any] | None:
        """Query vision LLM for next targeted click action."""
        state.record_transition("LLMActionPlan", f"Querying LLM (attempt {state.attempt})")
        try:
            result = await state.llm_manager.execute(
                Prompts.agentic_interact(
                    inspection["interact_html"],
                    state.attempt - 1,
                    aria_snapshot=inspection["aria_tree"],
                    viewport_width=inspection["vp_w"],
                    viewport_height=inspection["vp_h"],
                ),
                inspection["screenshot_b64"],
            )
            selector = result.get("action_selector")
            pixel_x = result.get("pixel_x")
            pixel_y = result.get("pixel_y")
            reason = result.get("reason", "—")

            state.last_selector = selector
            state.last_pixel = (int(pixel_x), int(pixel_y)) if (pixel_x is not None and pixel_y is not None) else None
            state.last_action_reason = reason

            reason_ctx = (reason + " " + (selector or "")).lower()
            state.is_unblocking_action = any(kw in reason_ctx for kw in [
                "cookie", "consent", "accept", "banner", "age", "gdpr",
                "overlay", "modal", "popup", "close", "dismiss",
            ])

            logger.info(
                f"[PlaybackGraph] LLM plan: selector={selector!r} "
                f"pixel={state.last_pixel} unblocking={state.is_unblocking_action} — {reason}"
            )
            return {
                "selector": selector,
                "pixel_x": pixel_x,
                "pixel_y": pixel_y,
                "reason": reason,
            }
        except Exception as e:
            logger.warning(f"[PlaybackGraph] LLM execution failed: {e}")
            return None

    async def node_execute_action(self, state: PlaybackState, plan: dict[str, Any]) -> bool:
        """Executes the action across multi-tiered handles (CSS -> Pixel -> Heuristic)."""
        cu = ComputerUse(state.page)
        selector = plan.get("selector")
        pixel_x = plan.get("pixel_x")
        pixel_y = plan.get("pixel_y")
        reason = plan.get("reason", "")
        clicked = False

        # Tier 1: CSS Selector
        if selector:
            clicked = await cu.click_by_selector(selector)
            if clicked:
                state.action_triggered = True
                logger.info(f"[PlaybackGraph] Clicked CSS selector: {selector!r} — {reason}")

        # Tier 2: LLM Pixel Coordinates
        if not clicked and pixel_x is not None and pixel_y is not None:
            try:
                clicked = await cu.click_at_pixel(int(pixel_x), int(pixel_y))
                if clicked:
                    state.action_triggered = True
                    logger.info(f"[PlaybackGraph] Clicked LLM pixel: ({pixel_x}, {pixel_y}) — {reason}")
                    await self.request_fullscreen_fn(state.page)
            except Exception as e:
                logger.debug(f"[PlaybackGraph] Tier 2 pixel click error: {e}")

        # Tier 3: Heuristic Pixel Search
        if not clicked:
            clicked = await cu.find_play_by_pixel()
            if clicked:
                state.action_triggered = True
                logger.info("[PlaybackGraph] Clicked heuristic pixel search")
                await self.request_fullscreen_fn(state.page)

        return clicked

    async def node_popup_recovery(self, state: PlaybackState, plan: dict[str, Any]) -> None:
        """Handles popups spawned by click-jacking and retries target clicks."""
        cu = ComputerUse(state.page)
        selector = plan.get("selector")
        px, py = plan.get("pixel_x"), plan.get("pixel_y")

        # Multi-click retry bounded to 4 attempts with smart polling
        for retry in range(1, 5):
            if await poll_until_playing(state.page, self.is_playing_fn, timeout_s=1.5):
                state.record_transition("PopupRecovery", f"Playback confirmed during popup recovery retry {retry}")
                break

            dismissed = await self.pre_pass_fn(state.page)
            saw_popup = state.popup_seen
            state.popup_seen = False
            state.action_triggered = True

            if dismissed or saw_popup:
                logger.info(f"[PlaybackGraph] Dismissed popup/overlay (retry {retry}/4) — re-clicking target")
            else:
                logger.info(f"[PlaybackGraph] Re-clicking target (retry {retry}/4)")

            await asyncio.sleep(0.3)
            if selector:
                await cu.click_by_selector(selector)
            elif px is not None and py is not None:
                await cu.click_at_pixel(int(px), int(py))
            else:
                await cu.find_play_by_pixel()

        # Record strategy if clicked
        if selector:
            state.remember_strategy(
                "llm_selector",
                f"LLM selector click on attempt {state.attempt}",
                {"selector": selector},
            )
        elif px is not None and py is not None:
            state.remember_strategy(
                "llm_pixel",
                f"LLM pixel click on attempt {state.attempt}",
                {"pixel_x": px, "pixel_y": py},
            )
        else:
            state.remember_strategy(
                "heuristic_pixel",
                f"heuristic pixel click on attempt {state.attempt}",
                {"heuristic_pixel": True},
            )

    async def node_post_click_recovery(self, state: PlaybackState) -> bool:
        """Run post-click unblocking and fallback direct play."""
        state.record_transition("PostClickRecovery", "Running post-click recovery")
        await self.pre_pass_fn(state.page)

        if await self.force_play_js_fn(state.page):
            state.remember_strategy("force_play_js", "post-click force video.play()")
            if await self.node_confirm_fullscreen(state, "post-click force video.play()"):
                return True

        if state.is_unblocking_action:
            if await self.try_direct_play_fn(state.page, max_click_retries=2):
                state.remember_strategy("direct_play", "post-click accessibility/direct play")
                if await self.node_confirm_fullscreen(state, "post-click accessibility/direct play"):
                    return True

        return False

    async def node_confirm_fullscreen(self, state: PlaybackState, reason: str) -> bool:
        """Verifies playback is active and requests fullscreen with page-refresh recovery."""
        if not await self.is_playing_fn(state.page):
            state.record_transition("FullscreenRejected", f"Playback not active after {reason}")
            return False

        state.playing_started = True
        # Bounded fullscreen confirmation (up to 4 attempts instead of 10)
        for fs_attempt in range(1, 5):
            logger.info(
                f"[PlaybackGraph] Video confirmed active after {reason}; "
                f"requesting fullscreen ({fs_attempt}/4)"
            )
            state.action_triggered = True
            state.nav_after_action = False

            await self.request_fullscreen_fn(state.page)
            # Smart poll: verify page didn't crash or stop playing
            await asyncio.sleep(1.0)

            if state.nav_after_action:
                logger.warning(
                    f"[PlaybackGraph] Page refreshed after fullscreen ({fs_attempt}/4); recovering playback"
                )
                state.nav_after_action = False
                state.action_triggered = False
                state.playing_started = False
                await asyncio.sleep(0.8)
                if await self.force_play_js_fn(state.page):
                    continue
                if await self.try_direct_play_fn(state.page, max_click_retries=2):
                    continue
                return False

            if await self.is_playing_fn(state.page):
                state.record_transition("PlaybackReady", "Playback active & fullscreen confirmed")
                return True

            logger.warning(f"[PlaybackGraph] Playback stopped after fullscreen request ({fs_attempt}/4); retrying")
            if await self.force_play_js_fn(state.page):
                continue
            if await self.try_direct_play_fn(state.page, max_click_retries=2):
                continue
            return False

        return False
