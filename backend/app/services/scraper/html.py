import logging
import re
from bs4 import BeautifulSoup

from app.services.scraper.media import _is_ad_video_url

logger = logging.getLogger("VideoScraper")

DROPPED_TAGS = [
    "script", "style", "noscript", "svg", "canvas",
    "header", "footer", "nav", "aside", "form",
    "ads", "advertisement",
]
DROPPED_ATTRS = ["style", "onclick", "onload", "data-tracking"]

# Attributes the LLM needs to name an element with a CSS selector or read its source.
INTERACTIVE_ATTRS = frozenset({
    "class", "id", "role", "aria-label", "aria-hidden",
    "type", "name", "value", "href", "src", "data-src",
    "data-video-id", "tabindex", "title",
})
_NON_INTERACTIVE_TAGS = ["script", "style", "noscript", "img", "picture",
                         "audio", "canvas", "svg", "meta", "link", "head"]
_PAGE_CHROME = ["header", "footer", "nav", "aside", "form"]
_PLAYER_ELEMENTS = ["video", "iframe", "object", "embed"]
_PLAYER_CLASS_RE = re.compile(r"player|jwplayer|vjs|plyr|flowplayer|mejs", re.I)
_DURATION_META = ("og:video:duration", "video:duration", "duration")
_PLAYER_BLOCK_MAX = 2500  # one player and its controls; a related-video grid is far bigger
_PLAYER_CLIMB = 4  # levels above the player element searched for its controls


def clean_html(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(DROPPED_TAGS):
        tag.decompose()

    for link in soup.find_all("link"):
        if not link or not hasattr(link, "attrs") or link.attrs is None:
            continue
        rel_attr = link.get("rel") or []
        rel = [r.lower() for r in rel_attr] if isinstance(rel_attr, list) else [str(rel_attr).lower()]
        if any(x in str(rel) for x in ["prefetch", "preconnect", "icon", "manifest", "preload"]):
            link.decompose()
            continue
        if "stylesheet" in rel and any(s in str(link.get("href", "")) for s in ["base-min", "non-critical", "cookies-modal"]):
            link.decompose()

    for meta in soup.find_all("meta"):
        if any(x in str(meta.get("name", "")).lower() for x in ["msapplication", "theme-color", "viewport"]):
            meta.decompose()

    for hidden in soup.find_all("input", type="hidden"):
        hidden.decompose()

    ignore_classes_ids = ["video__preview", "about-chips-list", "cookies-modal", "ad-detector", "asg-"]
    for pattern in ignore_classes_ids:
        for el in soup.find_all(class_=lambda x: x and pattern in x):
            el.decompose()
        for el in soup.find_all(id=lambda x: x and pattern in x):
            el.decompose()

    for tag in soup.find_all(True):
        for attr in DROPPED_ATTRS:
            tag.attrs.pop(attr, None)
        for attr in list(tag.attrs.keys()):
            if attr.startswith("data-") and attr not in ("data-src", "data-video-id", "data-duration"):
                del tag.attrs[attr]

    cleaned = str(soup)
    logger.debug(f"HTML cleaned: {len(html)} → {len(cleaned)} chars ({100*(1-len(cleaned)/max(len(html),1)):.0f}% reduction)")
    return cleaned


def _clean_for_interaction(html: str, max_len: int = 7000) -> str:
    """
    Interaction-focused HTML cleaner used by _agentic_interact.

    Crucially different from clean_html:
    - Does NOT remove cookie banners, consent dialogs, ad overlays — those are
      exactly the elements we need the LLM to see and click.
    - Strips only non-interactive noise (scripts, images, audio, canvas, svg).
    - Preserves class/id/role/aria attrs for CSS selector generation.
    """
    soup = BeautifulSoup(html, "html.parser")

    for tag in soup.find_all(_NON_INTERACTIVE_TAGS):
        tag.decompose()
    _keep_interactive_attrs(soup)
    text = _compact(str(soup))

    if len(text) > max_len:
        half = max_len // 2
        text = text[:half] + "\n...[truncated]...\n" + text[-half:]

    return text


def _keep_interactive_attrs(root) -> None:
    for tag in [root, *root.find_all(True)]:
        if getattr(tag, "attrs", None) is not None:
            tag.attrs = {k: v for k, v in tag.attrs.items() if k in INTERACTIVE_ATTRS}


def _compact(text: str) -> str:
    text = re.sub(r"\n\s*\n", "\n", text)
    return re.sub(r"  +", " ", text)


def _is_player_container(tag) -> bool:
    names = " ".join(tag.get("class") or []) + " " + (tag.get("id") or "")
    return bool(_PLAYER_CLASS_RE.search(names))


def _player_block(element):
    """The highest ancestor, a few levels up, that is still small: the player and its controls."""
    block = element
    for parent in list(element.parents)[:_PLAYER_CLIMB]:
        if parent.name in ("body", "html", "[document]", "main") or len(str(parent)) > _PLAYER_BLOCK_MAX:
            break
        block = parent
    return block


def _page_facts(soup: BeautifulSoup) -> list[str]:
    """The page title and any stated video length, as plain lines."""
    lines = []
    if soup.title and soup.title.get_text(strip=True):
        lines.append(f"Page title: {soup.title.get_text(strip=True)}")
    for key in _DURATION_META:
        tag = soup.find("meta", attrs={"property": key}) or soup.find("meta", attrs={"itemprop": key})
        if tag and tag.get("content"):
            lines.append(f"Stated duration: {tag['content']}")
            break
    return lines


def player_html(html: str, max_len: int = 4000) -> str:
    """
    The part of a page the main player lives in, for an LLM to pick selectors
    from: each video, embedded frame or player container with its nearby
    controls, plus the page title and stated length. Page chrome, scripts,
    related-video grids and ad frames are left out. A page with no player in
    its HTML yet falls back to its interactive elements.
    """
    soup = BeautifulSoup(html, "html.parser")
    facts = _page_facts(soup)
    for tag in soup.find_all(_NON_INTERACTIVE_TAGS + _PAGE_CHROME):
        tag.decompose()

    elements = [
        el for el in soup.find_all(_PLAYER_ELEMENTS)
        if not _is_ad_video_url(el.get("src") or el.get("data-src") or "")
    ] or [el for el in soup.find_all(True) if _is_player_container(el)]
    if not elements:
        return _clean_for_interaction(html, max_len)

    blocks: list = []
    for element in elements:
        block = _player_block(element)
        if any(block is b or b in block.parents for b in blocks):
            continue
        blocks = [b for b in blocks if block not in b.parents] + [block]

    parts = list(facts)
    for block in blocks:
        _keep_interactive_attrs(block)
        parts.append(_compact(str(block)))
    return "\n".join(parts)[:max_len]
