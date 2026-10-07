"""
Play search results in the app. A resolved stream gets an unguessable ID;
the backend then fetches the stream for the browser, adding the headers and
cookies the site expects. It only fetches the stream's own URL and the URLs
listed in the HLS playlists it served, and checks every redirect hop with
is_safe_url, so it never fetches an arbitrary link.
"""

from __future__ import annotations

import re
import threading
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx

from app.services.search.streams import ResolvedStream
from app.services.url_safety import is_safe_url

STREAM_TTL_S = 3600
_MAX_REDIRECTS = 5
_URI_ATTR = re.compile(r'URI="([^"]+)"')


@dataclass
class StreamEntry:
    stream: ResolvedStream
    created_at: float
    _allowed: set[str] = field(default_factory=set)

    def allows(self, url: str) -> bool:
        return url == self.stream.url or url in self._allowed

    def allow(self, urls: Iterable[str]) -> None:
        self._allowed.update(urls)


class StreamRegistry:
    def __init__(self, ttl_s: float = STREAM_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._entries: dict[str, StreamEntry] = {}
        self._lock = threading.Lock()

    def register(self, stream: ResolvedStream) -> str:
        stream_id = uuid.uuid4().hex
        with self._lock:
            cutoff = time.time() - self._ttl_s
            for old in [sid for sid, e in self._entries.items() if e.created_at < cutoff]:
                del self._entries[old]
            self._entries[stream_id] = StreamEntry(stream=stream, created_at=time.time())
        return stream_id

    def get(self, stream_id: str) -> StreamEntry | None:
        with self._lock:
            entry = self._entries.get(stream_id)
            if entry and entry.created_at < time.time() - self._ttl_s:
                del self._entries[stream_id]
                return None
            return entry


def is_playlist(url: str, content_type: str) -> bool:
    return "mpegurl" in content_type.lower() or url.split("?")[0].lower().endswith(".m3u8")


def rewrite_playlist(text: str, playlist_url: str, link: Callable[[str], str]) -> tuple[str, list[str]]:
    """
    The playlist with every URI (segments, sub-playlists, keys, init maps)
    replaced by link(absolute URI), plus the absolute URIs in order.
    """
    urls: list[str] = []

    def route(uri: str) -> str:
        absolute = urljoin(playlist_url, uri)
        urls.append(absolute)
        return link(absolute)

    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#"):
            lines.append(_URI_ATTR.sub(lambda m: f'URI="{route(m.group(1))}"', stripped))
        else:
            lines.append(route(stripped))
    return "\n".join(lines) + "\n", urls


class UpstreamRefused(Exception):
    """A redirect led to an unsafe host, or there were too many redirects."""


async def open_upstream(client: httpx.AsyncClient, url: str, headers: dict[str, str]) -> httpx.Response:
    """
    Send a streaming GET, following redirects by hand so each hop passes
    is_safe_url. The caller must close the response.
    """
    for _ in range(_MAX_REDIRECTS + 1):
        if not is_safe_url(url):
            raise UpstreamRefused(f"Unsafe stream URL: {url[:100]}")
        response = await client.send(client.build_request("GET", url, headers=headers), stream=True)
        if not response.is_redirect:
            return response
        await response.aclose()
        url = urljoin(url, response.headers["location"])
    raise UpstreamRefused("Too many redirects")


async def stream_reachable(client: httpx.AsyncClient, stream: ResolvedStream) -> bool:
    """True when the site serves the stream's first bytes to the backend."""
    headers = {**stream.request_headers(stream.url), "Range": "bytes=0-1"}
    try:
        response = await open_upstream(client, stream.url, headers)
    except (UpstreamRefused, httpx.HTTPError):
        return False
    await response.aclose()
    return response.status_code < 400
