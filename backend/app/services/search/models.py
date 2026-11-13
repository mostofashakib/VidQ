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
    score: int = 0  # the ranker's 0-3 relevance score
    votes: float = 0.0  # position votes from every result list that found it (see relevance.py)
    is_video: bool = False  # a source confirmed it is a video, so it needs no page check

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
