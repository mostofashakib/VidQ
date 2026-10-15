"""
Decide which media URL on a page is the main video.

A page loads many videos: the player, pre-rolls, floating ad sliders, looping
hover previews and trailers. The scraper sees all of them, so every URL is
scored against evidence from the page itself:

- The page's declared video names the real file: a schema.org VideoObject
  `contentUrl` in JSON-LD or microdata, or an OpenGraph/Twitter video file.
- The largest player outside any ad container is the main element.
- The page's numeric video ID (/video/90563/...) reappears in its media paths.
- Ad containers, looping muted previews and preview/trailer paths count against.

A candidate is trusted when at least one positive signal backs it. A file
whose length matches the page's declared length is trusted too, which keeps
a pre-roll playing in the main player from hiding the real stream. Untrusted
URLs are only worth trying when nothing on the page is trusted.

Trailers are the same content as the real video, only shorter, so ad rules
miss them. `vet_candidates` drops files whose probed length is well below the
page's declared length, drops trailer-named files when a plain one remains,
and prefers the longest probed file.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup

from app.services.scraper.media import _DIRECT_VIDEO_EXTENSIONS, _is_ad_video_url

# Class/id words that mark ad containers (matched as whole words).
_AD_MARKERS = frozenset({
    "ad", "ads", "advert", "advertisement", "adv", "sponsor", "sponsored",
    "promo", "banner", "preroll", "outstream", "exo", "popunder",
})
# Class/id words of non-ad clips that are still not the main video.
_PREVIEW_MARKERS = frozenset({"related", "thumb", "thumbnail", "preview", "trailer", "teaser"})
# Words that name a trailer in a path, label or container (whole words).
_TRAILER_WORDS = frozenset({"trailer", "preview", "teaser", "sample", "clip"})
_PREVIEW_PATH_RE = re.compile(r"(?<![a-z])(preview|trailer|teaser|thumb|tmb|sample)(?![a-z])")
_VIDEO_ID_RE = re.compile(r"^\d{4,}$")
_WORD_RE = re.compile(r"[a-z0-9]+")

# Pre-rolls and outstream ads run up to a minute.
_AD_MAX_SECONDS = 60
# A download shorter than this share of the expected length is not the video.
_MIN_EXPECTED_SHARE = 0.5
# A file within this share of the declared length is the declared video.
_LENGTH_MATCH_SHARE = 0.1
_MEDIA_PATH_SUFFIXES = tuple(_DIRECT_VIDEO_EXTENSIONS | {".mpd"})
_OPEN_GRAPH_VIDEO_PROPS = ("og:video:secure_url", "og:video:url", "og:video")
_ADAPTIVE_SUFFIXES = (".m3u8", ".mpd")
_ADAPTIVE_QUALITY = 100_000  # an adaptive stream carries every rendition, so it beats any single file
_RESOLUTION_RE = re.compile(r"(?<!\d)(\d{3,4})p(?![a-z])")

_SCORE_CONTENT_URL = 100
_SCORE_MAIN_ELEMENT = 60
_SCORE_VIDEO_ID = 40
_SCORE_PREVIEW = -50
_SCORE_PREVIEW_PATH = -30


@dataclass(frozen=True)
class VideoElement:
    """One <video> element as the browser reported it."""
    src: str
    area: int
    duration: float | None
    muted: bool
    loop: bool
    controls: bool
    container: str  # class and id names of the element and its ancestors
    index: int = -1  # position in the page, tagged as data-vidq-index
    label: str = ""  # <source label>, title and aria-label text


# Reads every <video> with the evidence used to spot ads, and tags each with
# data-vidq-index so a chosen element can be selected again later.
VIDEO_ELEMENTS_JS = """() => Array.from(document.querySelectorAll('video')).map((v, index) => {
    v.dataset.vidqIndex = String(index);
    const names = [];
    for (let n = v; n && names.length < 16; n = n.parentElement) {
        names.push(typeof n.className === 'string' ? n.className : '', n.id || '');
    }
    const source = v.querySelector('source');
    const labels = [v.getAttribute('title'), v.getAttribute('aria-label'),
        ...Array.from(v.querySelectorAll('source')).map(s => s.getAttribute('label'))];
    return {
        index,
        src: v.currentSrc || v.getAttribute('src') || (source ? source.getAttribute('src') : '') || '',
        area: (v.offsetWidth || 0) * (v.offsetHeight || 0),
        duration: Number.isFinite(v.duration) ? v.duration : null,
        muted: v.muted, loop: v.loop, controls: v.controls,
        container: names.join(' '),
        label: labels.filter(Boolean).join(' '),
    };
})"""


def parse_video_elements(raw_elements: list[dict], page_url: str) -> list[VideoElement]:
    """
    Turn VIDEO_ELEMENTS_JS output into VideoElements. Players without a source
    yet, or with a blob: source, are kept: they can still be the main player.
    """
    elements = []
    for raw in raw_elements:
        src = raw.get("src") or ""
        if src.startswith("/"):
            src = urljoin(page_url, src)
        elements.append(VideoElement(
            src=src, area=int(raw.get("area") or 0), duration=raw.get("duration"),
            muted=bool(raw.get("muted")), loop=bool(raw.get("loop")),
            controls=bool(raw.get("controls")), container=raw.get("container") or "",
            index=int(raw.get("index", -1)), label=raw.get("label") or "",
        ))
    return elements


@dataclass(frozen=True)
class PageVideoFacts:
    content_urls: tuple[str, ...]
    duration: float | None
    video_id: str | None


@dataclass(frozen=True)
class Candidate:
    url: str
    score: int
    trusted: bool
    reasons: tuple[str, ...]
    trailer_suspected: bool = False


@dataclass(frozen=True)
class Vetting:
    kept: list[Candidate]
    skipped: list[tuple[str, str]]
    only_trailers: bool  # every candidate was shorter than the declared length


def _parse_duration_seconds(raw_value) -> float | None:
    if raw_value is None:
        return None
    raw = str(raw_value).strip()
    if not raw:
        return None
    try:
        duration = float(raw)
        if 0 < duration < float("inf"):
            return duration
    except ValueError:
        pass
    if re.fullmatch(r"\d{1,2}:\d{2}(:\d{2})?", raw):
        parts = [float(part) for part in raw.split(":")]
        if len(parts) == 2:
            return parts[0] * 60 + parts[1]
        return parts[0] * 3600 + parts[1] * 60 + parts[2]
    iso_match = re.fullmatch(
        r"P(?:(?P<days>\d+)D)?(?:T)?(?:(?P<hours>\d+(?:\.\d+)?)H)?(?:(?P<minutes>\d+(?:\.\d+)?)M)?(?:(?P<seconds>\d+(?:\.\d+)?)S)?",
        raw.upper(),
    )
    if iso_match:
        duration = (
            float(iso_match.group("days") or 0) * 86400
            + float(iso_match.group("hours") or 0) * 3600
            + float(iso_match.group("minutes") or 0) * 60
            + float(iso_match.group("seconds") or 0)
        )
        return duration if duration > 0 else None
    return None


def _extract_html_duration(soup: BeautifulSoup | None) -> float | None:
    if not soup:
        return None
    candidates = []
    video_tag = soup.find("video", duration=True)
    if video_tag:
        candidates.append(video_tag.get("duration"))
    for attrs in (
        {"property": "og:video:duration"},
        {"property": "video:duration"},
        {"name": "duration"},
        {"itemprop": "duration"},
    ):
        tag = soup.find("meta", attrs={**attrs, "content": True})
        if tag:
            candidates.append(tag.get("content"))
    for candidate in candidates:
        duration = _parse_duration_seconds(candidate)
        if duration:
            return duration
    return None


def _json_ld_video_objects(soup: BeautifulSoup) -> list[dict]:
    """Every schema.org VideoObject in the page's JSON-LD, at any depth."""
    found: list[dict] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            types = node.get("@type")
            types = types if isinstance(types, list) else [types]
            if "VideoObject" in types:
                found.append(node)
            for value in node.values():
                walk(value)

    for script in soup.find_all("script", type="application/ld+json"):
        try:
            walk(json.loads(script.string or ""))
        except ValueError:
            continue
    return found


def _page_video_id(page_url: str) -> str | None:
    """The first all-digit path segment of 4+ digits, e.g. /video/90563/slug."""
    for segment in urlparse(page_url).path.split("/"):
        if _VIDEO_ID_RE.match(segment):
            return segment
    return None


def _is_video_object(tag) -> bool:
    return "videoobject" in (tag.get("itemtype") or "").lower()


def _microdata_content_urls(soup: BeautifulSoup) -> list[str]:
    """`contentUrl` of every microdata VideoObject, ignoring nested items' properties."""
    urls = []
    for item in soup.find_all(attrs={"itemscope": True}):
        if not _is_video_object(item):
            continue
        for prop in item.find_all(attrs={"itemprop": "contentUrl"}):
            if prop.find_parent(attrs={"itemscope": True}) is not item:
                continue
            urls.append(prop.get("content") or prop.get("href") or prop.get("src") or "")
    return urls


def _is_media_file(url: str, declared_type: str = "") -> bool:
    declared_type = declared_type.lower()
    return (
        declared_type.startswith("video/") or "mpegurl" in declared_type
        or urlparse(url).path.lower().endswith(_MEDIA_PATH_SUFFIXES)
    )


def _social_video_urls(soup: BeautifulSoup) -> list[str]:
    """OpenGraph and Twitter video tags that point at a media file, not an embed page."""
    def meta(key: str) -> str:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"name": key})
        return (tag.get("content") or "").strip() if tag else ""

    declared_type = meta("og:video:type")
    if declared_type and not _is_media_file("", declared_type):
        open_graph = []
    else:
        open_graph = [u for u in (meta(p) for p in _OPEN_GRAPH_VIDEO_PROPS) if u and _is_media_file(u, declared_type)]
    stream = meta("twitter:player:stream")
    return open_graph[:1] + ([stream] if stream else [])


def extract_page_facts(soup: BeautifulSoup | None, page_url: str) -> PageVideoFacts:
    objects = _json_ld_video_objects(soup) if soup else []
    declared = [obj.get("contentUrl") for obj in objects]
    if soup:
        declared += _microdata_content_urls(soup) + _social_video_urls(soup)
    content_urls = tuple(dict.fromkeys(
        url.strip() for url in declared if isinstance(url, str) and url.strip().startswith("http")
    ))
    duration = next(
        (d for d in (_parse_duration_seconds(obj.get("duration")) for obj in objects) if d),
        None,
    ) or _extract_html_duration(soup)
    return PageVideoFacts(content_urls=content_urls, duration=duration, video_id=_page_video_id(page_url))


def _words(text: str) -> set[str]:
    return set(_WORD_RE.findall(text.lower()))


def _same_file(url: str, other: str) -> bool:
    """Same host and file name. Signed path segments rotate on every page load."""
    a, b = urlparse(url), urlparse(other)
    name = a.path.rstrip("/").rsplit("/", 1)[-1]
    return bool(name) and a.hostname == b.hostname and name == b.path.rstrip("/").rsplit("/", 1)[-1]


def _has_video_id(url: str, video_id: str | None) -> bool:
    if not video_id:
        return False
    return re.search(rf"(?<!\d){re.escape(video_id)}(?!\d)", urlparse(url).path) is not None


def has_trailer_cue(url: str, label: str = "", container: str = "") -> bool:
    """A naming hint that url is a trailer. A suspicion, never proof on its own."""
    words = _words(urlparse(url).path) | _words(label) | _words(container)
    return bool(words & _TRAILER_WORDS)


def _is_ad_element(el: VideoElement) -> bool:
    return bool(_words(el.container) & _AD_MARKERS)


def main_element(elements: list[VideoElement]) -> VideoElement | None:
    """The largest <video> outside any ad container."""
    non_ad = [el for el in elements if not _is_ad_element(el)]
    return max(non_ad, key=lambda el: el.area) if non_ad else None


def _is_preview_element(el: VideoElement) -> bool:
    looping_muted_clip = el.loop and el.muted and not el.controls
    return looping_muted_clip or bool(_words(el.container) & _PREVIEW_MARKERS)


def rank_candidates(
    urls: list[str],
    elements: list[VideoElement],
    facts: PageVideoFacts,
) -> tuple[list[Candidate], list[tuple[str, str]]]:
    """
    Score each URL; return (ranked candidates best first, rejected (url, reason)).
    """
    ad_srcs = {el.src for el in elements if _is_ad_element(el)}
    main = main_element(elements)
    main_src = main.src if main else None
    by_src = {el.src: el for el in elements}

    ranked: list[Candidate] = []
    rejected: list[tuple[str, str]] = []
    for url in dict.fromkeys(u for u in urls if u and u.startswith("http")):
        if url in ad_srcs:
            rejected.append((url, "inside ad container"))
            continue
        if _is_ad_video_url(url):
            rejected.append((url, "known ad URL"))
            continue

        score, reasons = 0, []
        if any(_same_file(url, content) for content in facts.content_urls):
            score += _SCORE_CONTENT_URL
            reasons.append("matches the page's declared video")
        if url == main_src:
            score += _SCORE_MAIN_ELEMENT
            reasons.append("main player element")
        if _has_video_id(url, facts.video_id):
            score += _SCORE_VIDEO_ID
            reasons.append(f"contains page video id {facts.video_id}")
        trusted = score > 0

        element = by_src.get(url)
        if element and _is_preview_element(element):
            score += _SCORE_PREVIEW
            reasons.append("preview clip element")
        if _PREVIEW_PATH_RE.search(urlparse(url).path.lower()):
            score += _SCORE_PREVIEW_PATH
            reasons.append("preview-style path")

        trailer_suspected = (
            has_trailer_cue(url, element.label, element.container) if element else has_trailer_cue(url)
        )
        ranked.append(Candidate(
            url=url, score=score, trusted=trusted, reasons=tuple(reasons),
            trailer_suspected=trailer_suspected,
        ))

    ranked.sort(key=lambda c: c.score, reverse=True)
    return ranked, rejected


def is_trailer_length(length_s: float | None, declared_s: float | None) -> bool:
    """True when a file is well below the length the page declares."""
    return bool(declared_s) and length_s is not None and length_s < declared_s * _MIN_EXPECTED_SHARE


def _matches_declared_length(length_s: float | None, declared_s: float | None) -> bool:
    return bool(declared_s) and length_s is not None and abs(length_s - declared_s) <= declared_s * _LENGTH_MATCH_SHARE


def choose_candidates(
    ranked: list[Candidate],
    probed: dict[str, float | None],
    declared_s: float | None,
) -> list[Candidate]:
    """
    The candidates worth trying, best first. A file whose probed length
    matches the page's declared length becomes trusted. Trusted candidates
    are tried alone when there are any, otherwise every candidate is.
    """
    confirmed = [
        Candidate(
            url=c.url, score=c.score, trusted=True,
            reasons=c.reasons + (f"length matches the page's ~{declared_s:.0f}s",),
            trailer_suspected=c.trailer_suspected,
        ) if not c.trusted and _matches_declared_length(probed.get(c.url), declared_s) else c
        for c in ranked
    ]
    return [c for c in confirmed if c.trusted] or confirmed


def _quality(url: str) -> int:
    """Rank renditions of one video: an adaptive stream first, then the resolution in its name."""
    path = urlparse(url).path.lower()
    if path.endswith(_ADAPTIVE_SUFFIXES):
        return _ADAPTIVE_QUALITY
    match = _RESOLUTION_RE.search(path)
    return int(match.group(1)) if match else 0


def vet_candidates(
    candidates: list[Candidate],
    probed: dict[str, float | None],
    declared_s: float | None,
) -> Vetting:
    """
    Skip trailers, previews and ads before downloading. `probed` maps URLs to
    their remote length (None when the probe failed); `declared_s` is the
    page's stated length. Without one, the longest probed file is the
    reference: previews and pre-rolls sit next to the real file and are far
    shorter. Kept candidates come longest first (renditions of the same
    length best quality first), unprobed last.
    """
    longest = max((probed[c.url] for c in candidates if probed.get(c.url)), default=None)
    kept: list[Candidate] = []
    skipped: list[tuple[str, str]] = []
    for c in candidates:
        length = probed.get(c.url)
        if is_trailer_length(length, declared_s):
            skipped.append((c.url, f"{length:.0f}s file but the page declares ~{declared_s:.0f}s"))
        elif not declared_s and is_trailer_length(length, longest):
            skipped.append((c.url, f"{length:.0f}s file next to a {longest:.0f}s one"))
        else:
            kept.append(c)

    if any(not c.trailer_suspected for c in kept):
        skipped.extend((c.url, "named like a trailer") for c in kept if c.trailer_suspected)
        kept = [c for c in kept if not c.trailer_suspected]

    def order(c: Candidate) -> tuple:
        length = probed.get(c.url)
        if length is None:
            return (2, 0, 0.0)
        near_longest = longest is not None and length >= longest * (1 - _LENGTH_MATCH_SHARE)
        return (0 if near_longest else 1, -_quality(c.url), -length)

    kept.sort(key=order)
    return Vetting(kept=kept, skipped=skipped, only_trailers=bool(candidates) and not kept)


def is_acceptable_download(
    trusted: bool,
    downloaded_s: float | None,
    expected_s: float | None,
) -> tuple[bool, str]:
    """Return (accepted, reason) for a finished download."""
    if downloaded_s is None:
        return (True, "") if trusted else (False, "unreadable length from an untrusted source")
    if is_trailer_length(downloaded_s, expected_s):
        return False, f"{downloaded_s:.0f}s but the page expected ~{expected_s:.0f}s"
    if not expected_s and not trusted and downloaded_s < _AD_MAX_SECONDS:
        return False, f"{downloaded_s:.0f}s ad-length clip from an untrusted source"
    return True, ""
