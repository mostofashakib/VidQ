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
            '  "direct_video_url": "direct stream URL found in HTML, or null",\n'
            '  "duration": "main video duration in seconds if visible in HTML/text, or null"\n'
            "}\n\n"
            "HTML (truncated):\n"
            + html[:12000]
        )

    @staticmethod
    def agentic_interact(
        interactive_html: str,
        attempt: int = 0,
        aria_snapshot: str = "",
        viewport_width: int = 1920,
        viewport_height: int = 1080,
    ) -> str:
        """
        Prompt for the agentic playback loop.
        Accepts an optional aria_snapshot (compact YAML-like ARIA tree) that
        gives the LLM a semantic view of all interactive elements — more
        reliable than parsing raw HTML for non-standard players.
        viewport_width/viewport_height are the exact pixel dimensions of the
        attached screenshot so the LLM can return accurate coordinates.
        """
        context = (
            f"ATTEMPT {attempt + 1}: the previous click did NOT start playback. "
            "Look more carefully — something is still blocking or the wrong element was chosen.\n\n"
            if attempt > 0
            else ""
        )
        aria_section = (
            "\nACCESSIBILITY TREE (ARIA snapshot — use this first for precise element identification):\n"
            + aria_snapshot[:3000]
            + "\n"
            if aria_snapshot
            else ""
        )
        return (
            "You are a browser automation agent. Your only goal: find the ONE element to click "
            "RIGHT NOW that will make the video play.\n\n"
            f"{context}"
            f"SCREENSHOT INFO: The attached screenshot is EXACTLY {viewport_width}×{viewport_height} pixels "
            f"and matches the browser viewport 1:1. Pixel (0,0) is the TOP-LEFT corner; "
            f"pixel ({viewport_width - 1},{viewport_height - 1}) is the BOTTOM-RIGHT corner. "
            "All pixel_x/pixel_y values you return MUST be integers within these bounds.\n\n"
            "INTERACTION TIERS — work through these in order:\n\n"
            "TIER 1 — ACCESSIBILITY TREE (if provided)\n"
            "Check the ARIA snapshot first. It lists every interactive element with its role and "
            "accessible name. Use it to identify:\n"
            "  • Consent buttons (name: 'Accept', 'Accept All', 'OK', 'Agree', 'Got It')\n"
            "  • Ad skip/close buttons (name contains 'Skip', 'Close', 'Dismiss')\n"
            "  • Play buttons (role=button, name contains 'Play')\n"
            "If found in the ARIA tree, set action_selector to the most specific CSS selector "
            "that targets the same element.\n\n"
            "TIER 2 — HTML TAGS (fall through when ARIA tree is absent or incomplete)\n"
            "1. COOKIE / CONSENT BANNER  — look for a <div>/<section> whose class/id contains "
            "   'cookie', 'consent', 'gdpr', 'notice', 'banner', or 'popup'. "
            "   Inside it find an <a>/<button> whose text is 'OK', 'Accept', 'Accept All', "
            "   'I Accept', 'Agree', 'Got It', or 'Allow'. Return its most specific selector.\n"
            "2. AD TIMER / SKIP BUTTON  — any element whose class/id contains "
            "   'skip', 'skip-ad', 'ad-close', 'countdown', 'timer'.\n"
            "3. AD OVERLAY CLOSE BUTTON  — button/span with aria-label='Close' or class "
            "   containing 'close'/'dismiss' inside an 'ad'/'overlay'/'modal' parent.\n"
            "4. VIDEO PLAY BUTTON OVERLAY  — <button>/<div> with class/id containing "
            "   'play', 'big-play', 'vjs-big-play-button', 'jw-display-icon', 'play-btn', "
            "   or aria-label containing 'play'.\n"
            "5. VIDEO PLAYER CONTROLS PLAY  — button inside '.controls'/'.vjs-control-bar' "
            "   whose aria-label or title contains 'Play'.\n"
            "6. The <video> element itself (clicking often toggles play).\n\n"
            "TIER 3 — SCREENSHOT PIXEL COORDINATES (when CSS/ARIA cannot reach the element)\n"
            f"The screenshot is {viewport_width}×{viewport_height} pixels. "
            "If you can SEE the play button, consent button, or skip button visually but it "
            "lives inside a sandboxed iframe, canvas, or has no usable CSS selector, return "
            "its CENTER pixel position as pixel_x (0 = left edge) and pixel_y (0 = top edge). "
            "Be precise — identify the exact center of the clickable icon, not the surrounding container. "
            "Leave pixel_x/pixel_y null if a working CSS selector covers the element.\n\n"
            "IMPORTANT RULES:\n"
            "- If you see a consent banner or ad overlay, ALWAYS handle it first before trying to play.\n"
            "- Return the MOST SPECIFIC CSS selector possible for action_selector.\n"
            f"- pixel_x must be 0–{viewport_width - 1}; pixel_y must be 0–{viewport_height - 1}.\n"
            "- Do NOT set action_selector to null if a CSS selector exists.\n"
            "- Do NOT provide pixel coordinates if a CSS selector is sufficient.\n\n"
            "Return ONLY valid JSON — no markdown, no <think> block, no extra text:\n"
            '{"action_selector": "selector or null", '
            '"pixel_x": null, "pixel_y": null, '
            '"reason": "one sentence why"}\n'
            + aria_section
            + "\nINTERACTIVE HTML:\n"
            + interactive_html
        )

    @staticmethod
    def cloudflare_bypass() -> str:
        """
        Vision prompt: the LLM looks at a Cloudflare challenge screenshot and
        returns the pixel coordinates of the checkbox / button to click.
        """
        return (
            "You are a browser automation agent. The browser is currently showing a Cloudflare "
            "security verification page (Turnstile, 'Verifying you are human', or similar).\n\n"
            "Look at the screenshot. Find the ONE interactive element that a human would click "
            "to pass the check. This is typically:\n"
            "  • A small checkbox on the left side of a compact widget labelled "
            "'I am human' or similar\n"
            "  • A 'Verify you are human' button\n"
            "  • A 'Click to continue' link\n\n"
            "Return the CENTER pixel position of that element in the 1920×1080 viewport.\n\n"
            "Return ONLY valid JSON — no markdown, no explanation:\n"
            '{"pixel_x": <integer or null>, "pixel_y": <integer or null>, '
            '"reason": "one sentence describing what you found"}'
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
