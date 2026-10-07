"""Shared result type for every search source."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SearchResult:
    url: str
    title: str
    source: str  # which source found it, e.g. "youtube", "duckduckgo"
    duration: float | None = None
    thumbnail: str = ""
    snippet: str = ""
    reason: str = ""  # why the ranker picked it
    hits: int = 1  # how many source/query results were this same video

    def to_dict(self) -> dict:
        return {
            "url": self.url,
            "title": self.title,
            "source": self.source,
            "duration": self.duration,
            "thumbnail": self.thumbnail,
            "snippet": self.snippet,
            "reason": self.reason,
        }
