"""
Resolve a search result's page to a stream the browser can play. yt-dlp
picks one H.264 format, preferring a single file over HLS. The stream keeps
the headers and cookies the site expects, so the backend can fetch it on the
browser's behalf.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from urllib.parse import urlparse

from app.services.search.sources import Runner, SourceError, _run_ytdlp

# H.264 plays in every browser; a direct file needs no playlist handling.
STREAM_FORMAT = "b[vcodec^=avc1][protocol^=http]/b[vcodec^=avc1]/b[protocol^=http]/b"
_FORWARDED_HEADERS = ("User-Agent", "Referer", "Origin")
_COOKIE_ATTRIBUTES = frozenset({"domain", "path", "expires", "max-age", "secure", "httponly", "samesite"})

Cookie = tuple[str, str, str]  # (name, value, domain)


class StreamUnavailable(Exception):
    """The page has no stream yt-dlp can resolve."""


def parse_ytdlp_cookies(text: str) -> tuple[Cookie, ...]:
    """Cookies from yt-dlp's `cookies` field: 'name=value; Domain=...; Path=/; ...' repeated."""
    cookies: list[list[str]] = []
    for part in text.split(";"):
        key, _, value = part.strip().partition("=")
        if not key:
            continue
        if key.lower() not in _COOKIE_ATTRIBUTES:
            cookies.append([key, value, ""])
        elif key.lower() == "domain" and cookies:
            cookies[-1][2] = value.lstrip(".").lower()
    return tuple((name, value, domain) for name, value, domain in cookies)


def _ytdlp_reason(message: str) -> str:
    """yt-dlp's error without the 'ERROR:' prefix and its bug-report boilerplate."""
    return message.removeprefix("ERROR:").split("; please report")[0].strip()


def _domain_matches(host: str, domain: str) -> bool:
    return bool(domain) and (host == domain or host.endswith("." + domain))


@dataclass(frozen=True)
class ResolvedStream:
    url: str
    kind: str  # "file" | "hls"
    headers: dict[str, str]
    cookies: tuple[Cookie, ...]
    duration: float | None

    def request_headers(self, url: str) -> dict[str, str]:
        """Headers for fetching url (the stream or one of its parts)."""
        host = (urlparse(url).hostname or "").lower()
        headers = dict(self.headers)
        cookie = "; ".join(f"{name}={value}" for name, value, domain in self.cookies if _domain_matches(host, domain))
        if cookie:
            headers["Cookie"] = cookie
        return headers


def stream_from_info(info: dict) -> ResolvedStream:
    headers = info.get("http_headers") or {}
    duration = info.get("duration")
    return ResolvedStream(
        url=info["url"],
        kind="hls" if str(info.get("protocol", "")).startswith("m3u8") else "file",
        headers={k: v for k, v in headers.items() if k in _FORWARDED_HEADERS},
        cookies=parse_ytdlp_cookies(info.get("cookies") or ""),
        duration=float(duration) if isinstance(duration, (int, float)) else None,
    )


async def resolve_stream(page_url: str, runner: Runner = _run_ytdlp) -> ResolvedStream:
    try:
        output = await runner([
            "yt-dlp", "-j", "--no-playlist", "--no-warnings", "--socket-timeout", "20",
            "-f", STREAM_FORMAT, page_url,
        ])
    except SourceError as exc:
        raise StreamUnavailable(_ytdlp_reason(str(exc))) from None
    lines = output.strip().splitlines()
    try:
        info = json.loads(lines[-1])
        if not str(info.get("url", "")).startswith("http"):
            raise ValueError("no stream URL")
        return stream_from_info(info)
    except (IndexError, ValueError, AttributeError) as exc:
        raise StreamUnavailable(f"No playable stream: {exc}") from None
