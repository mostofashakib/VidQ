"""
A simplified PageRank for search results.

Every result list (one source answering one query) is a page that links to
its results, and a link counts less the lower the result sits:
1 / (VOTE_K + position). Copies of one video found by several sources,
queries or sites pass their votes to the single result kept for it (dedupe
adds them up), so a video many lists put near the top gathers the most.

The LLM's 0-3 relevance score decides first. Votes order results that share
a score, so the most relevant matches are on top and the most widely found
lead each tier.
"""

from __future__ import annotations

import dataclasses

from app.services.search.models import SearchResult

VOTE_K = 10  # damping: position 1 earns 1/11, position 10 about half that


def position_votes(results: list[SearchResult]) -> list[SearchResult]:
    """The list's results, each carrying the vote its position earns."""
    return [dataclasses.replace(r, votes=1 / (VOTE_K + i)) for i, r in enumerate(results, start=1)]


def by_relevance(results: list[SearchResult]) -> list[SearchResult]:
    """Highest LLM score first, then most votes. Ties keep their order."""
    return sorted(results, key=lambda r: (-r.score, -r.votes))
