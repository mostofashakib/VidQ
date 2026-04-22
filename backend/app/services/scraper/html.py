import logging
import re
from bs4 import BeautifulSoup

logger = logging.getLogger("VideoScraper")

DROPPED_TAGS = [
    "script", "style", "noscript", "svg", "canvas",
    "header", "footer", "nav", "aside", "form",
    "ads", "advertisement",
]
DROPPED_ATTRS = ["style", "onclick", "onload", "data-tracking"]


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

    for tag in soup.find_all(["script", "style", "noscript", "img", "picture",
                               "audio", "canvas", "svg", "meta", "link", "head"]):
        tag.decompose()

    KEEP_ATTRS = {
        "class", "id", "role", "aria-label", "aria-hidden",
        "type", "name", "value", "href", "src", "data-src",
        "data-video-id", "tabindex", "title",
    }
    for tag in soup.find_all(True):
        tag.attrs = {k: v for k, v in tag.attrs.items() if k in KEEP_ATTRS}

    text = str(soup)
    text = re.sub(r"\n\s*\n", "\n", text)
    text = re.sub(r"  +", " ", text)

    if len(text) > max_len:
        half = max_len // 2
        text = text[:half] + "\n...[truncated]...\n" + text[-half:]

    return text
