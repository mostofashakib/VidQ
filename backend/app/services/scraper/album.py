"""
Expand album pages, pages that list their videos as plain HTML players,
into one direct video file per player.

A page counts as an album when its static HTML holds one or more distinct
video files outside ad containers and hover previews. Each file is fetched
later with the album page as Referer, which album media hosts require.
Trailers are dropped: trailer-named players when a plain one exists, and
files far shorter than the page's declared length. A single video must also
be reachable with that Referer. Anything else (a page built by JavaScript,
or a file that needs the site's cookies) is left to the regular pipeline.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from app.services.scraper.candidates import (
    VideoElement,
    _is_ad_element,
    _is_preview_element,
    extract_page_facts,
    has_trailer_cue,
    is_trailer_length,
)
from app.services.scraper.media import _DIRECT_VIDEO_EXTENSIONS, _is_ad_video_url, probe_remote_duration
from app.services.url_safety import is_safe_url

logger = logging.getLogger("VideoScraper")

_MAX_REDIRECTS = 5
_FETCH_TIMEOUT_S = 10
# Class/id names from this many ancestors decide whether a player is an ad.
_CONTAINER_DEPTH = 8

# (url, referer, user_agent=...) -> remote length in seconds, or None.
LengthProbe = Callable[..., float | None]


@dataclass(frozen=True)
class AlbumItem:
    url: str
    thumbnail: str
    title: str


@dataclass(frozen=True)
class Album:
    title: str
    page_url: str
    items: list[AlbumItem]
    declared_duration: float | None = None  # the page's stated video length


def _is_direct_file(url: str) -> bool:
    path = urlparse(url).path.lower()
    return url.startswith("http") and any(path.endswith(ext) for ext in _DIRECT_VIDEO_EXTENSIONS)


def _best_source(video: Tag, page_url: str) -> str | None:
    """The player's highest-resolution direct file, from src or <source> tags."""
    options = []
    for tag in [video, *video.find_all("source")]:
        src = (tag.get("src") or "").strip()
        if not src:
            continue
        url = urljoin(page_url, src)
        if _is_direct_file(url):
            res = str(tag.get("res") or tag.get("size") or "0")
            options.append((int(res) if res.isdigit() else 0, url))
    if not options:
        return None
    best_res = max(res for res, _ in options)
    return next(url for res, url in options if res == best_res)


def _container_names(video: Tag) -> str:
    names = []
    for node in [video, *list(video.parents)[:_CONTAINER_DEPTH]]:
        if isinstance(node, Tag):
            names.extend(node.get("class") or [])
            names.append(node.get("id") or "")
    return " ".join(names)


def _labels(video: Tag) -> str:
    texts = [video.get("title"), video.get("aria-label")]
    texts += [source.get("label") for source in video.find_all("source")]
    return " ".join(t for t in texts if t)


def _album(title: str, page_url: str, files: list[tuple[str, str]], declared: float | None) -> Album:
    """Build an Album from (url, poster) pairs, numbering titles when there are several."""
    total = len(files)
    items = [
        AlbumItem(url=url, thumbnail=poster, title=f"{title} ({n}/{total})" if total > 1 else title)
        for n, (url, poster) in enumerate(files, start=1)
    ]
    return Album(title=title, page_url=page_url, items=items, declared_duration=declared)


def _page_title(soup: BeautifulSoup, page_url: str) -> str:
    og = soup.find("meta", property="og:title", content=True)
    if og and og["content"].strip():
        return og["content"].strip()
    if soup.title and soup.title.text.strip():
        return soup.title.text.strip()
    return page_url


def parse_album(html: str, page_url: str) -> Album | None:
    """Return the page's album, or None when it holds no direct video players."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, tuple[str, bool]] = {}  # file URL → (poster, trailer cue), in page order
    for video in soup.find_all("video"):
        url = _best_source(video, page_url)
        if not url or url in found or _is_ad_video_url(url):
            continue
        container = _container_names(video)
        element = VideoElement(
            src=url, area=0, duration=None,
            muted=video.has_attr("muted"), loop=video.has_attr("loop"),
            controls=video.has_attr("controls"), container=container,
        )
        if _is_ad_element(element) or _is_preview_element(element):
            continue
        poster = (video.get("poster") or "").strip()
        cue = has_trailer_cue(url, label=_labels(video), container=container)
        found[url] = (urljoin(page_url, poster) if poster else "", cue)

    if not found:
        return None
    plain = [(url, poster) for url, (poster, cue) in found.items() if not cue]
    files = plain or [(url, poster) for url, (poster, _) in found.items()]
    declared = extract_page_facts(soup, page_url).duration
    return _album(_page_title(soup, page_url), page_url, files, declared)


def _vet_album(album: Album, user_agent: str, probe: LengthProbe) -> Album | None:
    """
    Drop files far shorter than the declared length (trailers). A lone video
    must be reachable with the album page as Referer, or the regular pipeline,
    which has the site's cookies, should handle the page instead.
    """
    lengths: dict[str, float | None] = {}

    def length(url: str) -> float | None:
        if url not in lengths:
            lengths[url] = probe(url, album.page_url, user_agent=user_agent)
        return lengths[url]

    items = album.items
    if album.declared_duration:
        items = [i for i in items if not is_trailer_length(length(i.url), album.declared_duration)]
    if not items:
        return None
    if len(items) == 1 and length(items[0].url) is None:
        return None
    files = [(i.url, i.thumbnail) for i in items]
    return _album(album.title, album.page_url, files, album.declared_duration)


def fetch_album(
    url: str,
    user_agent: str,
    client: httpx.Client | None = None,
    probe: LengthProbe = probe_remote_duration,
) -> Album | None:
    """
    Fetch url over plain HTTP and parse it as an album. Every redirect hop is
    checked with is_safe_url before it is requested. Returns None on any
    failure so the caller falls back to the single-video pipeline.
    """
    owns_client = client is None
    client = client or httpx.Client(timeout=_FETCH_TIMEOUT_S)
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            if not is_safe_url(url):
                logger.warning(f"Album fetch refused unsafe URL: {url[:100]}")
                return None
            resp = client.get(url, headers={"User-Agent": user_agent}, follow_redirects=False)
            if resp.is_redirect:
                url = urljoin(url, resp.headers["location"])
                continue
            content_type = resp.headers.get("content-type", "")
            if resp.status_code != 200 or "text/html" not in content_type:
                return None
            album = parse_album(resp.text, url)
            return _vet_album(album, user_agent, probe) if album else None
        return None
    except httpx.HTTPError as exc:
        logger.info(f"Album fetch failed for {url[:100]}: {exc}")
        return None
    finally:
        if owns_client:
            client.close()
