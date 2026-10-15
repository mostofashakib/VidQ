"""
Read a video player's stream URL straight from page HTML, without a browser.

Players such as JW Player, video.js, Clappr and Plyr take their source from an
inline script, e.g. `sources: [{file: "https://.../master.m3u8"}]`. Many video
hosts pack that script as eval(function(p,a,c,k,e,d){...}), which yt-dlp's
decoder unpacks. Article pages often hold no player of their own but embed a
host's player page in an iframe, so when the page has no sources its iframes
are read the same way, with the Referer a browser would send.

Other players fetch their source from an API when the page loads and only
request the stream once playback starts. The browser stage reads those small
fetch/XHR answers with `api_media_urls`, so the stream is known even when
playback never starts.

Every quoted URL in a script or API answer whose path ends in a media
extension counts as a source. Known ad URLs are dropped; the caller checks
lengths against the page.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup
from yt_dlp.utils import decode_packed_codes

from app.services.scraper.candidates import PageVideoFacts, extract_page_facts
from app.services.scraper.media import _is_ad_video_url, browser_referer
from app.services.url_safety import is_safe_url

logger = logging.getLogger("VideoScraper")

_MEDIA_SUFFIXES = (".m3u8", ".mp4", ".mpd", ".webm")
_QUOTED_RE = re.compile(r"""["']([^"'\s<>]+)["']""")
_PACKED_MARKER = "p,a,c,k,e,"
_MAX_FRAMES = 3
_API_RESOURCE_TYPES = frozenset({"fetch", "xhr"})
_API_CONTENT_TYPES = ("application/json", "text/plain", "text/json")
_MAX_REDIRECTS = 5
_FETCH_TIMEOUT_S = 15


@dataclass(frozen=True)
class PlayerStream:
    url: str
    player_page: str  # the page whose script named the stream; its URL is the Referer


@dataclass(frozen=True)
class PlayerPage:
    streams: list[PlayerStream]
    facts: PageVideoFacts  # what the page states about its video, to vet the streams

    @classmethod
    def empty(cls) -> "PlayerPage":
        return cls(streams=[], facts=PageVideoFacts(content_urls=(), duration=None, video_id=None))


def _scripts(html: str) -> list[str]:
    """Inline script bodies, with packed scripts unpacked."""
    bodies = []
    for script in BeautifulSoup(html, "html.parser").find_all("script", src=False):
        body = script.string or script.get_text() or ""
        if _PACKED_MARKER in body:
            try:
                body = decode_packed_codes(body)
            except (AttributeError, ValueError, IndexError) as exc:
                logger.debug(f"Packed script could not be decoded: {exc}")
        bodies.append(body)
    return bodies


def _is_media_path(url: str) -> bool:
    return urlparse(url).path.lower().endswith(_MEDIA_SUFFIXES)


def _media_urls_in_text(text: str, base_url: str) -> list[str]:
    """Quoted media URLs in script or JSON text, absolute, without ads or repeats."""
    found: dict[str, None] = {}
    for raw in _QUOTED_RE.findall(text.replace("\\/", "/")):
        url = urljoin(base_url, raw)
        if url.startswith("http") and _is_media_path(url) and not _is_ad_video_url(url):
            found.setdefault(url)
    return list(found)


def player_sources(html: str, page_url: str) -> list[str]:
    """Media URLs named in the page's scripts, absolute, in page order."""
    found: dict[str, None] = {}
    for body in _scripts(html):
        for url in _media_urls_in_text(body, page_url):
            found.setdefault(url)
    return list(found)


def is_api_answer(resource_type: str, content_type: str) -> bool:
    """A fetch/XHR response with JSON or text: what a player's source API returns."""
    return resource_type in _API_RESOURCE_TYPES and content_type.lower().startswith(_API_CONTENT_TYPES)


def api_media_urls(resource_type: str, content_type: str, body: str, url: str) -> list[str]:
    """Media URLs in a player's API answer, or [] for any other response."""
    return _media_urls_in_text(body, url) if is_api_answer(resource_type, content_type) else []


def player_frames(html: str, page_url: str) -> list[str]:
    """Absolute iframe sources that could hold a player, in page order."""
    frames: dict[str, None] = {}
    for iframe in BeautifulSoup(html, "html.parser").find_all("iframe"):
        src = urljoin(page_url, (iframe.get("src") or iframe.get("data-src") or "").strip())
        if src.startswith("http") and not _is_ad_video_url(src):
            frames.setdefault(src)
    return list(frames)


async def _get_html(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> tuple[str, str] | None:
    """(final URL, HTML) of a page, checking every redirect hop; None on failure."""
    for _ in range(_MAX_REDIRECTS + 1):
        if not is_safe_url(url):
            logger.warning(f"Player config fetch refused unsafe URL: {url[:100]}")
            return None
        response = await client.get(url, headers=headers, follow_redirects=False)
        if response.is_redirect:
            url = urljoin(url, response.headers["location"])
            continue
        if response.status_code != 200 or "html" not in response.headers.get("content-type", ""):
            return None
        return url, response.text
    return None


async def find_player_streams(
    page_url: str,
    user_agent: str,
    client: httpx.AsyncClient | None = None,
) -> PlayerPage:
    """Streams named by the page's player config, or by its embedded players'."""
    owns_client = client is None
    client = client or httpx.AsyncClient(timeout=_FETCH_TIMEOUT_S)
    try:
        page = await _get_html(client, page_url, {"User-Agent": user_agent})
        if page is None:
            return PlayerPage.empty()
        final_url, html = page
        facts = extract_page_facts(BeautifulSoup(html, "html.parser"), final_url)
        streams = [PlayerStream(url=u, player_page=final_url) for u in player_sources(html, final_url)]
        if not streams:
            for frame_url in player_frames(html, final_url)[:_MAX_FRAMES]:
                headers = {"User-Agent": user_agent, "Referer": browser_referer(final_url, frame_url)}
                frame = await _get_html(client, frame_url, headers)
                if frame:
                    frame_final, frame_html = frame
                    streams += [PlayerStream(url=u, player_page=frame_final)
                                for u in player_sources(frame_html, frame_final)]
        return PlayerPage(streams=streams, facts=facts)
    except httpx.HTTPError as exc:
        logger.info(f"Player config fetch failed for {page_url[:100]}: {exc}")
        return PlayerPage.empty()
    finally:
        if owns_client:
            await client.aclose()
