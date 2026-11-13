"""
Keep only search results that lead to a video.

Engines list articles, galleries and forum threads next to videos. A result
counts as a video when a source confirmed it (yt-dlp search returns only
videos), or when its page declares or plays a video: a schema.org
VideoObject, OpenGraph or Twitter video tags, a media URL in a player script,
or an embedded player frame. A URL yt-dlp recognises is not proof on its
own, since some of its extractors match any post or article on a site. Raw
<video> tags do not count, since listing pages play hover previews in them.

A page that refuses the check (403, a paywall, a timeout) stays, since many
real video sites block plain fetches. Missing pages (404, 410) and pages with
nothing to play are dropped.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections.abc import Callable
from urllib.parse import urlparse

import httpx
from bs4 import BeautifulSoup

from app.services.scraper.candidates import _is_video_object, _json_ld_video_objects, meta_content
from app.services.scraper.player_config import fetch_page, player_frames, player_sources
from app.services.search.dedupe import video_key
from app.services.search.models import SearchResult
from app.services.url_safety import is_safe_url

logger = logging.getLogger("VideoSearch")

_VIDEO_META = ("og:video", "og:video:url", "og:video:secure_url", "twitter:player", "twitter:player:stream")
_PLAYER_PATH_RE = re.compile(r"/(?:embed|embedframe|player|e)(?:/|$)")
_GONE = frozenset({404, 410})
_MEDIA_TYPES = ("video/", "application/x-mpegurl", "application/vnd.apple.mpegurl", "application/dash+xml")
_TIMEOUT_S = 10
_CONCURRENCY = 8


def _is_player_frame(src: str) -> bool:
    return video_key(src) is not None or bool(_PLAYER_PATH_RE.search(urlparse(src).path.lower()))


def has_video_evidence(html: str, page_url: str) -> bool:
    """True when the page declares a video or carries a player. Cheapest checks first."""
    soup = BeautifulSoup(html, "html.parser")
    if _json_ld_video_objects(soup) or any(_is_video_object(t) for t in soup.find_all(attrs={"itemscope": True})):
        return True
    if meta_content(soup, "og:type").lower().startswith("video") or any(meta_content(soup, k) for k in _VIDEO_META):
        return True
    return bool(player_sources(html, page_url)) or any(_is_player_frame(f) for f in player_frames(html, page_url))


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(timeout=_TIMEOUT_S)


class VideoPageCheck:
    """Filters search results down to the ones that lead to a video."""

    def __init__(self, user_agent: str, client_factory: Callable[[], httpx.AsyncClient] = _client) -> None:
        self._headers = {"User-Agent": user_agent}
        self._client_factory = client_factory

    async def video_pages(self, results: list[SearchResult]) -> list[SearchResult]:
        """The results that lead to a video, in their order."""
        semaphore = asyncio.Semaphore(_CONCURRENCY)
        async with self._client_factory() as client:
            async def check(result: SearchResult) -> bool:
                if result.is_video:
                    return True
                async with semaphore:
                    return await self._is_video(client, result.url)

            verdicts = await asyncio.gather(*(check(r) for r in results))
        return [r for r, ok in zip(results, verdicts) if ok]

    async def _is_video(self, client: httpx.AsyncClient, url: str) -> bool:
        if not is_safe_url(url):
            return False
        try:
            response = await fetch_page(client, url, self._headers)
        except httpx.HTTPError as exc:
            logger.debug(f"Video check could not load {url[:100]}; keeping it: {exc}")
            return True
        if response is None or response.status_code in _GONE:
            logger.info(f"Dropped {url[:100]}: page missing or unsafe")
            return False
        if response.status_code != 200:
            return True
        content_type = response.headers.get("content-type", "").lower()
        if content_type.startswith(_MEDIA_TYPES):
            return True
        # Parsing a large page takes a while, so it runs off the event loop.
        if "html" not in content_type or not await asyncio.to_thread(
            has_video_evidence, response.text, str(response.url)
        ):
            logger.info(f"Dropped {url[:100]}: not a video page")
            return False
        return True
