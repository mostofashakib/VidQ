"""
Find a page's own embeddable player through oEmbed discovery: the page links
to an oEmbed endpoint, which answers with the player's iframe. The app shows
that player when it cannot relay the video stream itself.
"""

from __future__ import annotations

import json
import logging
import random
from urllib.parse import urljoin

import httpx
from bs4 import BeautifulSoup

from app.services.scraper.playback import USER_AGENTS
from app.services.search.playback import UpstreamRefused, open_upstream
from app.services.url_safety import is_safe_url

logger = logging.getLogger("VideoSearch")

_MAX_BODY_BYTES = 2_000_000


async def _read(client: httpx.AsyncClient, url: str, user_agent: str) -> bytes | None:
    """The body of a 200 answer (at most _MAX_BODY_BYTES), or None."""
    try:
        response = await open_upstream(client, url, {"User-Agent": user_agent})
    except (UpstreamRefused, httpx.HTTPError) as exc:
        logger.info(f"oEmbed fetch failed for {url[:100]}: {exc}")
        return None
    try:
        if response.status_code != 200:
            return None
        body = bytearray()
        async for chunk in response.aiter_bytes():
            body += chunk
            if len(body) >= _MAX_BODY_BYTES:
                break
        return bytes(body)
    finally:
        await response.aclose()


async def discover_embed(client: httpx.AsyncClient, page_url: str) -> str | None:
    """The https iframe source of the page's oEmbed player, or None."""
    user_agent = random.choice(USER_AGENTS)
    page = await _read(client, page_url, user_agent)
    if page is None:
        return None
    link = BeautifulSoup(page, "html.parser").find("link", type="application/json+oembed", href=True)
    if link is None:
        return None
    answer = await _read(client, urljoin(page_url, link["href"]), user_agent)
    try:
        player_html = json.loads(answer or b"").get("html") or ""
    except (ValueError, AttributeError):
        return None
    iframe = BeautifulSoup(player_html, "html.parser").find("iframe", src=True)
    src = iframe["src"].strip() if iframe else ""
    return src if src.startswith("https://") and is_safe_url(src) else None
