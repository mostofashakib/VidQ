"""
Media URLs seen while a page loads, each with the document that requested it.

Pages often play their video in an embedded player from another site. The
browser fetches that player's files with the player's own page as Referer,
and video hosts that check Referer refuse the outer page. Probes and
downloads send the Referer of the document that asked for each file.
"""

from __future__ import annotations

from app.services.scraper.media import browser_referer


class MediaSources:
    def __init__(self, page_url: str) -> None:
        self._page_url = page_url
        self._documents: dict[str, str] = {}

    def add(self, media_url: str, document_url: str | None = None) -> None:
        """Record media_url; the first document to request it wins."""
        if not (document_url or "").startswith("http"):
            document_url = self._page_url  # about:blank and srcdoc frames share the page's
        self._documents.setdefault(media_url, document_url)

    def urls(self) -> list[str]:
        return list(self._documents)

    def referer(self, media_url: str) -> str:
        """The Referer a browser sends for media_url from the document that requested it."""
        return browser_referer(self._documents.get(media_url, self._page_url), media_url)

    def __len__(self) -> int:
        return len(self._documents)
