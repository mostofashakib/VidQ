"""
Title, description, thumbnail and length for a downloaded video's library entry.

Video pages state these in schema.org VideoObject, OpenGraph, Twitter and
plain <title>/<meta> tags, so they are read straight from the HTML. The LLM
is asked only when the page states no title, and it gets a short summary of
the page (stated facts, headings, opening text), never the full HTML or a
screenshot.
"""

from __future__ import annotations

import logging

from bs4 import BeautifulSoup

from app.services.prompts import Prompts
from app.services.scraper.candidates import _json_ld_video_objects, extract_page_facts, meta_content
from app.services.search.page_reader import page_text

logger = logging.getLogger("VideoScraper")

_TITLE_META = ("og:title", "twitter:title")
_DESCRIPTION_META = ("og:description", "description", "twitter:description")
_THUMBNAIL_META = ("og:image", "twitter:image")
_HEADINGS = ["h1", "h2", "h3"]
_MAX_HEADINGS = 8
_PAGE_TEXT_CHARS = 1500


def _first_text(value) -> str:
    """A JSON-LD value as text: the first item of a list, a dict's url, or the string itself."""
    if isinstance(value, list):
        value = value[0] if value else ""
    if isinstance(value, dict):
        value = value.get("url") or value.get("contentUrl") or ""
    return " ".join(str(value or "").split())


def _first_meta(soup: BeautifulSoup, keys: tuple[str, ...]) -> str:
    return next((v for v in (meta_content(soup, k) for k in keys) if v), "")


def _heading(soup: BeautifulSoup) -> str:
    tag = soup.title or soup.find("h1")
    return " ".join(tag.get_text(" ").split()) if tag else ""


def page_metadata(html: str, page_url: str) -> dict:
    """What the page states about its video. Missing fields are "" (duration None)."""
    soup = BeautifulSoup(html or "", "html.parser")
    video = next(iter(_json_ld_video_objects(soup)), {})
    return {
        "title": _first_text(video.get("name")) or _first_meta(soup, _TITLE_META) or _heading(soup),
        "description": _first_text(video.get("description")) or _first_meta(soup, _DESCRIPTION_META),
        "thumbnail": _first_text(video.get("thumbnailUrl")) or _first_meta(soup, _THUMBNAIL_META),
        "duration": extract_page_facts(soup, page_url).duration,
    }


def _page_summary(html: str, facts: dict) -> str:
    """Stated facts, headings and opening text: enough to name a video, nothing more."""
    soup = BeautifulSoup(html or "", "html.parser")
    headings = [" ".join(h.get_text(" ").split()) for h in soup.find_all(_HEADINGS)[:_MAX_HEADINGS]]
    lines = [f"{key}: {value}" for key, value in facts.items() if value]
    lines += [f"heading: {h}" for h in headings if h]
    lines.append(f"text: {page_text(html or '', max_chars=_PAGE_TEXT_CHARS)}")
    return "\n".join(lines)


async def video_metadata(llm_manager, html: str, page_url: str, video_url: str) -> dict:
    """
    Metadata for the library: the page's stated facts, with the LLM filling
    the title and description only when the page states no title. An LLM
    failure leaves them empty for the caller's fallback.
    """
    facts = page_metadata(html, page_url)
    if not facts["title"] and html and llm_manager:
        try:
            answer = await llm_manager.execute_text(Prompts.video_metadata(_page_summary(html, facts)))
            facts["title"] = str(answer.get("title") or "").strip()
            facts["description"] = facts["description"] or str(answer.get("description") or "").strip()
        except Exception as exc:
            logger.warning(f"LLM metadata extraction failed for {page_url[:100]}: {exc}")
    return {**facts, "video_url": video_url}
