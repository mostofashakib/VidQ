"""
Centralized prompt templates for all LLM calls in the pipeline.
"""

class Prompts:
    @staticmethod
    def navigation_selectors(cleaned_html: str) -> str:
        """
        Text-only prompt (no image). Used in the Fast Pass to ask the LLM
        to identify the CSS selectors for the Play and Fullscreen buttons.
        Now also asks for a candidate direct video URL found in the HTML.
        """
        return (
            "You are a web automation expert. Identify the CSS selectors and primary video link for the MAIN video.\n"
            "How to find the MAIN video:\n"
            "1. Priority: Look for containers named 'player', 'main', or 'content'. Primary video content is generally found within the '.main' class if present.\n"
            "2. Context: The main video is usually the standalone player in the central content area. Sidebars/grids are related content and should be ignored.\n"
            "3. Metadata: Look for a video URL (src, data-src) that seems to be the primary stream.\n\n"
            "Rules:\n"
            "- Return ONLY a JSON object:\n"
            "{\n"
            '  "play_selector": "string or null",\n'
            '  "fullscreen_selector": "string or null",\n'
            '  "settings_selector": "string or null",\n'
            '  "quality_selector": "string or null",\n'
            '  "main_video_selector": "string or null",\n'
            '  "direct_video_url": "string or null"\n'
            "}\n\n"
            "HTML:\n"
            + cleaned_html
        )

    @staticmethod
    def resolve_stuck_vision(html_context: str) -> str:
        """
        Vision prompt for resolving an interaction block.
        """
        return (
            "I'm trying to play a video but I'm 'stuck'. Looking at the screenshot and HTML:\n"
            "1. Identify if there is an ad-blocker detector, cookie banner, or overlay blocking the view.\n"
            "2. Identify the correct button to click to dismiss the block or start the video.\n\n"
            "Return ONLY a JSON object with a single best CSS selector:\n"
            '{"action_selector": "string or null"}\n\n'
            "HTML (truncated):\n"
            + html_context[:6000]
        )

    @staticmethod
    def video_metadata(cleaned_html: str, network_video_urls: list) -> str:
        """
        Vision prompt for final metadata extraction.
        """
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
            "Return ONLY a JSON object with keys: video_url, title, description, duration, thumbnail.\n\n"
            "HTML:\n"
            + cleaned_html
            + "\n\nNetwork video URLs:\n"
            + str(network_video_urls)[:2000]
        )

    @staticmethod
    def navigation_selectors_vision(html: str) -> str:
        """
        Vision variant of navigation_selectors.
        Used by the agent to visually confirm the main player block.
        """
        return (
            "You are a web automation expert. Looking at the screenshot and HTML, "
            "identify the CSS selectors and primary video link for the MAIN video.\n"
            "How to find the MAIN video:\n"
            "1. Priority: Look for containers named 'player', 'main', or 'content'. Primary video content is generally found within the '.main' class if present.\n"
            "2. Visual Context: The main video is usually the standalone player in the central content area (occupying most of the space). Sidebars/grids are related content and should be ignored.\n"
            "3. Metadata: Look for a video URL (src, data-src) that seems to be the primary stream.\n\n"
            "Rules:\n"
            "- Return ONLY a JSON object:\n"
            "{\n"
            '  "play_selector": "string or null",\n'
            '  "fullscreen_selector": "string or null",\n'
            '  "settings_selector": "string or null",\n'
            '  "quality_selector": "string or null",\n'
            '  "main_video_selector": "string or null",\n'
            '  "direct_video_url": "string or null"\n'
            "}\n\n"
            "HTML (truncated):\n"
            + html[:12000]
        )
