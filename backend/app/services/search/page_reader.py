"""
Read search engine and video pages as compact content.

`strip_page` removes everything that is not content: scripts, styles, media
and frames, page chrome (header, nav, footer, forms), hidden elements, ad
containers, and every attribute except links, titles and (on request) image
sources.
`harvest_results` turns an engine's result page into SearchResults by
following its outbound links, so it does not depend on the engine's markup.
"""

from __future__ import annotations

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
