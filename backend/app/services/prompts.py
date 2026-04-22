"""
Centralized prompt templates for all LLM calls in the pipeline.
"""

class Prompts:
    @staticmethod
    def navigation_selectors_vision(html: str) -> str:
        """
        Vision+HTML prompt → CSS selectors for the main player.
        Called once at the start of each extraction pass.
        """
        return (
            "You are a web automation expert. Using the HTML (and screenshot if visible), "
            "identify CSS selectors and the primary video link for the MAIN video player.\n\n"
            "Rules for the MAIN video:\n"
            "1. It occupies the largest area on screen / is the central focal point.\n"
            "2. Ignore sidebars, related-video grids, and thumbnail carousels.\n"
            "3. Look for a direct video URL in src, data-src, or data-video-id attributes.\n\n"
            "Return ONLY valid JSON — no markdown, no explanation:\n"
            "{\n"
            '  "play_selector": "CSS selector for the play button, or null",\n'
            '  "fullscreen_selector": "CSS selector for fullscreen button, or null",\n'
            '  "settings_selector": "CSS selector for settings/gear button, or null",\n'
            '  "quality_selector": "CSS selector for quality option, or null",\n'
            '  "main_video_selector": "CSS selector for the <video> element, or null",\n'
            '  "direct_video_url": "direct stream URL found in HTML, or null"\n'
            "}\n\n"
            "HTML (truncated):\n"
            + html[:12000]
        )

    @staticmethod
    def agentic_interact(interactive_html: str, attempt: int = 0) -> str:
        """
        Prompt for the agentic playback loop.
        Optimised for text-only models (qwen3.x) — reasoning from HTML alone.
        Each attempt gets a fresh page snapshot.
        """
        context = (
            f"ATTEMPT {attempt + 1}: the previous click did NOT start playback. "
            "Look more carefully — something is still blocking or the wrong element was chosen.\n\n"
            if attempt > 0
            else ""
        )
        return (
            "You are a browser automation agent. Your only goal: find the ONE element to click "
            "RIGHT NOW that will make the video play.\n\n"
            f"{context}"
            "Work through this checklist IN ORDER and pick the FIRST match:\n\n"
            "1. COOKIE / CONSENT BANNER  — look for a <div> or <section> whose class or id contains "
            "   'cookie', 'consent', 'gdpr', 'notice', 'banner', or 'popup'. "
            "   Inside it find an <a> or <button> whose text is exactly 'OK', 'Accept', 'Accept All', "
            "   'I Accept', 'Agree', 'Got It', or 'Allow'. Return its most specific selector.\n\n"
            "2. AD TIMER / SKIP BUTTON  — look for any element whose class or id contains "
            "   'skip', 'skip-ad', 'ad-close', 'countdown', 'timer'. "
            "   If a skip/close button exists (even if greyed out), click it.\n\n"
            "3. AD OVERLAY CLOSE BUTTON  — look for a button/span with "
            "   aria-label='Close' or class containing 'close' or 'dismiss' "
            "   that is a child of an element whose class contains 'ad', 'overlay', or 'modal'.\n\n"
            "4. VIDEO PLAY BUTTON OVERLAY  — a <button> or <div> with class/id containing "
            "   'play', 'big-play', 'vjs-big-play-button', 'jw-display-icon', 'play-btn', "
            "   or with aria-label containing 'play'. This is often the large ▶ icon on the video.\n\n"
            "5. VIDEO PLAYER CONTROLS PLAY  — a button inside a '.controls', '.player-controls', "
            "   or '.vjs-control-bar' whose aria-label or title contains 'Play'.\n\n"
            "6. The <video> element itself (last resort — clicking it often toggles play).\n\n"
            "IMPORTANT RULES:\n"
            "- Return the MOST SPECIFIC selector possible (e.g. '.cookie-bar .accept-btn' not just 'button').\n"
            "- If you see a consent banner, ALWAYS handle it first before trying to play.\n"
            "- Do NOT return null unless absolutely nothing interactive is visible.\n\n"
            "Return ONLY valid JSON — no markdown, no <think> block, no extra text:\n"
            '{"action_selector": "selector", "reason": "one sentence why"}\n\n'
            "INTERACTIVE HTML:\n"
            + interactive_html
        )

    @staticmethod
    def video_metadata(cleaned_html: str, network_video_urls: list) -> str:
        """Vision prompt for final metadata extraction."""
        return (
            "You are an expert at extracting video metadata. Identify the MAIN video on the page.\n"
            "Heuristics:\n"
            "- The main video is typically standalone and prominent.\n"
            "- Ignore 'related videos' or 'clusters' in sidebars.\n\n"
            "Extract:\n"
            "- video_url: the direct or main embedded URL\n"
            "- title: the main video title\n"
            "- duration: seconds (integer)\n"
            "- description: 2-sentence summary\n"
            "- thumbnail: the primary thumbnail URL\n\n"
            "Return ONLY valid JSON with keys: video_url, title, description, duration, thumbnail.\n\n"
            "HTML:\n"
            + cleaned_html
            + "\n\nNetwork video URLs:\n"
            + str(network_video_urls)[:2000]
        )
