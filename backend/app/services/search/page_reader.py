"""
Read search engine and video pages as compact content.

`strip_page` removes everything that is not content: scripts, styles, media
and frames, page chrome (header, nav, footer, forms), hidden elements, ad
containers, and every attribute except links, titles and (on request) image
sources.
`harvest_results` turns an engine's result page into SearchResults by
following its outbound links, so it does not depend on the engine's markup.
`bing_results` reads engines that keep each result card in a JSON attribute.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urljoin, urlparse

from bs4 import BeautifulSoup, Tag

from app.services.scraper.candidates import _AD_MARKERS, _parse_duration_seconds, _words
from app.services.search.dedupe import canonical_url, unwrap_redirect
from app.services.search.models import SearchResult

_NOISE_TAGS = [
    "script", "style", "noscript", "template", "link", "meta", "svg", "canvas",
    "iframe", "object", "embed", "video", "audio", "picture", "source",
    "header", "nav", "footer", "aside", "form", "input", "button", "select", "textarea",
]
_IMAGE_ATTRS = ("src", "data-src")
_HEADINGS = ["h1", "h2", "h3", "h4", "h5", "h6"]
_DURATION_RE = re.compile(r"\b(?:\d{1,2}:)?\d{1,2}:\d{2}\b")
_BLOCK_MAX_DEPTH = 5
_SNIPPET_CHARS = 300
_TITLE_MAX_CHARS = 200
_MIN_TITLE_CHARS = 3


def _is_hidden(tag: Tag) -> bool:
    style = (tag.get("style") or "").replace(" ", "").lower()
    return (
        tag.has_attr("hidden")
        or tag.get("aria-hidden") == "true"
        or "display:none" in style
        or "visibility:hidden" in style
    )


def _is_ad(tag: Tag) -> bool:
    names = " ".join(tag.get("class") or []) + " " + (tag.get("id") or "")
    return bool(_words(names) & _AD_MARKERS)


def strip_page(html: str, keep_images: bool = False) -> BeautifulSoup:
    """The page reduced to content: text, links, titles and optionally image sources."""
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all(_NOISE_TAGS):
        tag.decompose()
    for tag in soup.find_all(True):
        if not tag.decomposed and (_is_hidden(tag) or _is_ad(tag)):
            tag.decompose()
    if not keep_images:
        for img in soup.find_all("img"):
            img.decompose()
    for tag in soup.find_all(True):
        keep = ("href", "title") + (_IMAGE_ATTRS if tag.name == "img" else ())
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in keep}
    return soup


def page_text(html: str, max_chars: int = 2000) -> str:
    """Visible text of the page on one line, at most max_chars long."""
    text = " ".join(strip_page(html).get_text(" ").split())
    return text[:max_chars]


def _base_domain(url: str) -> str:
    host = urlparse(url).hostname or ""
    return ".".join(host.split(".")[-2:])


def _is_on(url: str, domain: str) -> bool:
    host = urlparse(url).hostname or ""
    return host == domain or host.endswith("." + domain)


def _clean_text(node: Tag) -> str:
    return " ".join(node.get_text(" ").split())


def _result_block(anchor: Tag, canonical: str, page_url: str) -> Tag:
    """The largest ancestor (a few levels up) whose links all point at this result."""
    block = anchor
    node = anchor.parent
    for _ in range(_BLOCK_MAX_DEPTH):
        if not isinstance(node, Tag) or node.name in ("body", "html", "[document]"):
            break
        targets = {
            canonical_url(unwrap_redirect(urljoin(page_url, a["href"])))
            for a in node.find_all("a", href=True)
        }
        if targets - {canonical, None}:
            break
        block = node
        node = node.parent
    return block


def _title(block: Tag, anchors: list[Tag]) -> str:
    """
    A card's own title: an element's title attribute, then a heading, then the
    longest link text. Link text alone often includes the length and view count.
    """
    for el in block.find_all(True):
        title = " ".join((el.get("title") or "").split())
        if title:
            return title
    heading = block.find(_HEADINGS)
    if heading and _clean_text(heading):
        return _clean_text(heading)
    return max((_clean_text(a) for a in anchors), key=len)


def _thumbnail(block: Tag, page_url: str) -> str:
    """First image in the block, unwrapped from engine image proxies."""
    for img in block.find_all("img"):
        for attr in _IMAGE_ATTRS:
            src = img.get(attr) or ""
            if src and not src.startswith("data:"):
                return unwrap_redirect(urljoin(page_url, src))
    return ""


def harvest_results(html: str, page_url: str, source: str) -> list[SearchResult]:
    """
    One SearchResult per outbound link on an engine's result page, in page
    order. Links back to the engine itself and links without visible text are
    skipped. Title, snippet, duration and thumbnail come from the link's block.
    """
    soup = strip_page(html, keep_images=True)
    engine = _base_domain(page_url)
    anchors: dict[str, list[tuple[str, Tag]]] = {}  # canonical → [(target, anchor)]
    for a in soup.find_all("a", href=True):
        target = unwrap_redirect(urljoin(page_url, a["href"]))
        canonical = canonical_url(target)
        if not canonical or _is_on(target, engine):
            continue
        anchors.setdefault(canonical, []).append((target, a))

    results = []
    for canonical, links in anchors.items():
        block = _result_block(links[0][1], canonical, page_url)
        title = _title(block, [a for _, a in links])[:_TITLE_MAX_CHARS]
        if len(title) < _MIN_TITLE_CHARS:
            continue
        text = _clean_text(block)
        duration_match = _DURATION_RE.search(text)
        results.append(SearchResult(
            url=links[0][0],
            title=title,
            source=source,
            duration=_parse_duration_seconds(duration_match.group(0)) if duration_match else None,
            thumbnail=_thumbnail(block, page_url),
            snippet=text.replace(title, "", 1).strip()[:_SNIPPET_CHARS],
        ))
    return results


def _json_attr(tag: Tag, name: str) -> dict:
    try:
        value = json.loads(tag.get(name) or "")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def bing_results(html: str, page_url: str, source: str) -> list[SearchResult]:
    """
    One SearchResult per Bing video card, in page order. Each card holds its
    title, length and page link in a `vrhm` JSON attribute and its thumbnail
    in `mmeta`. Cards without a page link and repeats are skipped.
    """
    results: dict[str, SearchResult] = {}
    for card in BeautifulSoup(html, "html.parser").find_all(attrs={"vrhm": True}):
        meta, media = _json_attr(card, "vrhm"), _json_attr(card, "mmeta")
        url = meta.get("pgurl") or meta.get("murl") or ""
        title = " ".join(str(meta.get("vt") or "").split())[:_TITLE_MAX_CHARS]
        canonical = canonical_url(url) if url.startswith("http") else None
        if not canonical or canonical in results or len(title) < _MIN_TITLE_CHARS:
            continue
        results[canonical] = SearchResult(
            url=url,
            title=title,
            source=source,
            duration=_parse_duration_seconds(meta.get("du")),
            thumbnail=media.get("turl") or meta.get("smturl") or "",
        )
    return list(results.values())


def yandex_results(html: str, page_url: str, source: str) -> list[SearchResult]:
    """
    One SearchResult per Yandex video card (.VideoSnippet), in page order.
    Extracts the destination video link, title, duration, and thumbnail.
    """
    results: dict[str, SearchResult] = {}
    soup = BeautifulSoup(html, "html.parser")
    for snippet in soup.select(".VideoSnippet"):
        title_el = snippet.select_one(".VideoSnippet-Title")
        title = " ".join(title_el.get_text(" ").split())[:_TITLE_MAX_CHARS] if title_el else ""
        if len(title) < _MIN_TITLE_CHARS:
            continue

        target_url = None
        host_link = snippet.select_one("a.VideoHostExtended-Host, a[class*='Host']")
        if host_link and host_link.get("href"):
            href = host_link["href"]
            if href.startswith("http") and not _is_on(href, "yandex.com") and not _is_on(href, "yandex.ru"):
                target_url = href

        if not target_url:
            for a in snippet.find_all("a", href=True):
                href = a["href"]
                if href.startswith("http") and not _is_on(href, "yandex.com") and not _is_on(href, "yandex.ru"):
                    target_url = href
                    break

        if not target_url:
            continue

        canonical = canonical_url(target_url)
        if not canonical or canonical in results:
            continue

        duration = None
        duration_el = snippet.select_one(".VideoThumb3-Duration, [class*='Duration'], .VideoSnippet-Duration")
        if duration_el:
            duration = _parse_duration_seconds(duration_el.get_text(strip=True))
        if not duration:
            m = _DURATION_RE.search(snippet.get_text())
            if m:
                duration = _parse_duration_seconds(m.group(0))

        thumb = ""
        thumb_el = snippet.select_one("video[poster], img[src]")
        if thumb_el:
            thumb = thumb_el.get("poster") or thumb_el.get("src") or ""
            if thumb.startswith("//"):
                thumb = "https:" + thumb

        text = _clean_text(snippet)
        snippet_text = text.replace(title, "", 1).strip()[:_SNIPPET_CHARS]

        results[canonical] = SearchResult(
            url=target_url,
            title=title,
            source=source,
            duration=duration,
            thumbnail=thumb,
            snippet=snippet_text,
            is_video=True,
        )
    return list(results.values())

