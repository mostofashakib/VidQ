"""
Collapse duplicate search results. Three layers decide that two results are
the same video:

1. Canonical URL: tracking parameters, fragments, www./m. prefixes and search
   engine redirect wrappers removed.
2. Video ID: yt-dlp's URL patterns map every URL form of a video on a known
   site (watch?v=, youtu.be, /shorts/) to one key, without a network call.
3. Mirror: a similar title with a length within a few seconds on another
   site. Sites retitle copies, so titles match when they share most of their
   distinctive words. On one site, matching titles are separate uploads.
"""

from __future__ import annotations

import dataclasses
import re
from functools import lru_cache
from urllib.parse import parse_qsl, unquote, urlencode, urlparse, urlunparse

from yt_dlp.extractor import gen_extractor_classes

from app.services.search.models import SearchResult

_TRACKING_PARAMS = frozenset({"fbclid", "gclid", "msclkid", "si", "feature", "ref", "ref_src", "spm"})
_REDIRECT_PARAMS = ("uddg", "url", "q", "u")
_HOST_PREFIXES = ("www.", "m.")
_MIRROR_MAX_GAP_S = 3.0
_MIRROR_MIN_SHARED_WORDS = 3
_MIRROR_MIN_SHARED_SHARE = 0.5  # of the shorter title's distinctive words
_TITLE_STOPWORDS = frozenset({"the", "and", "for", "with", "from", "you", "are", "this", "that", "video", "videos"})
_EXTRACTORS = [ie for ie in gen_extractor_classes() if ie.ie_key() != "Generic"]


def unwrap_redirect(url: str) -> str:
    """The destination of a search engine redirect link, or url itself."""
    for key, value in parse_qsl(urlparse(url.strip()).query):
        if key in _REDIRECT_PARAMS and unquote(value).startswith("http"):
            return unwrap_redirect(unquote(value))
    return url.strip()


def canonical_url(url: str) -> str | None:
    """A stable form of url for comparison, or None when it is not an http link."""
    parsed = urlparse(unwrap_redirect(url))
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        return None

    host = parsed.hostname.lower()
    for prefix in _HOST_PREFIXES:
        if host.startswith(prefix):
            host = host[len(prefix):]
    if parsed.port:
        host = f"{host}:{parsed.port}"
    path = parsed.path.rstrip("/") or "/"
    query = urlencode(sorted(
        (k, v) for k, v in parse_qsl(parsed.query)
        if not k.startswith("utm_") and k not in _TRACKING_PARAMS
    ))
    return urlunparse((parsed.scheme, host, path, "", query, ""))


@lru_cache(maxsize=4096)
def video_key(url: str) -> str | None:
    """'<site>:<video id>' when a yt-dlp extractor recognises url, else None."""
    for ie in _EXTRACTORS:
        try:
            if ie.suitable(url):
                return f"{ie.ie_key()}:{ie._match_id(url)}"
        except Exception:
            continue
    return None


def _title_words(title: str) -> frozenset[str]:
    return frozenset(w for w in re.findall(r"[a-z0-9]+", title.lower()) if len(w) >= 3 and w not in _TITLE_STOPWORDS)


def _similar_titles(a: frozenset[str], b: frozenset[str]) -> bool:
    shared = len(a & b)
    return shared >= _MIRROR_MIN_SHARED_WORDS and shared >= _MIRROR_MIN_SHARED_SHARE * min(len(a), len(b))


def _site(url: str) -> str:
    host = urlparse(unwrap_redirect(url)).hostname or ""
    return ".".join(host.split(".")[-2:])


class SeenIndex:
    """Results already kept, matched by all three layers."""

    def __init__(self) -> None:
        self._by_key: dict[str, int] = {}
        self._mirrors: list[tuple[frozenset[str], float, str, int]] = []  # (title words, length, site, position)
        self.results: list[SearchResult] = []

    @staticmethod
    def _keys(result: SearchResult) -> set[str]:
        keys = set()
        canonical = canonical_url(result.url)
        if canonical:
            keys.add(f"url:{canonical}")
        vid = video_key(result.url)
        if vid:
            keys.add(f"id:{vid}")
        return keys

    def find(self, result: SearchResult) -> int | None:
        """Position of the kept result that result duplicates, or None."""
        for key in self._keys(result):
            if key in self._by_key:
                return self._by_key[key]
        if result.duration is not None:
            words, site = _title_words(result.title), _site(result.url)
            for other_words, duration, other_site, position in self._mirrors:
                if (other_site != site and abs(duration - result.duration) <= _MIRROR_MAX_GAP_S
                        and _similar_titles(words, other_words)):
                    return position
        return None

    def add(self, result: SearchResult) -> int:
        position = len(self.results)
        self.results.append(result)
        for key in self._keys(result):
            self._by_key.setdefault(key, position)
        if result.duration is not None:
            self._mirrors.append((_title_words(result.title), result.duration, _site(result.url), position))
        return position


def _merge(kept: SearchResult, duplicate: SearchResult) -> SearchResult:
    """Fill fields the kept result lacks from its duplicate."""
    return dataclasses.replace(
        kept,
        duration=kept.duration if kept.duration is not None else duplicate.duration,
        thumbnail=kept.thumbnail or duplicate.thumbnail,
        snippet=kept.snippet or duplicate.snippet,
        hits=kept.hits + duplicate.hits,
    )


def dedupe(results: list[SearchResult], seen: SeenIndex | None = None) -> list[SearchResult]:
    """
    Keep the first of each set of duplicates, in order, filling its missing
    fields from the others and counting them in `hits`. Results matching
    `seen` (earlier pages) are dropped.
    """
    local = SeenIndex()
    for result in results:
        if seen is not None and seen.find(result) is not None:
            continue
        position = local.find(result)
        if position is None:
            local.add(result)
        else:
            local.results[position] = _merge(local.results[position], result)
    return local.results
