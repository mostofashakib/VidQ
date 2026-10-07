"""
The search agent. For a description of the video a user wants:

1. The LLM plans a few search queries.
2. Every source runs every query (in parallel); failing sources are skipped.
3. Results are deduplicated against each other and against earlier pages.
4. The candidates found most often and closest to the description go to the
   LLM, which scores each one; the rest wait in a backlog for later pages.
   Local models slow down sharply past about 20 candidates per call. Scoring
   every candidate keeps the model from stopping at the first literal match.
5. Results are shown PAGE_SIZE at a time; "more" serves the ranked pool first
   and fetches the next page from every source only when the pool runs low.
6. Before a result is shown, it is compared by its frames with every shown
   result of nearly the same length on another site. Sites retitle copies,
   so a matching picture is what proves a duplicate.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import re
import time
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Protocol
from urllib.parse import urlparse

from app.services.prompts import Prompts
from app.services.search.dedupe import SeenIndex, _site, dedupe
from app.services.search.models import SearchResult
from app.services.search.sources import SearchSource

logger = logging.getLogger("VideoSearch")

PAGE_SIZE = 5
MAX_QUERIES = 5
MIN_MATCH_SCORE = 2  # of 3: "likely matches"
RANK_CANDIDATE_CAP = 20  # candidates per ranking call; the rest wait in the backlog
_MAX_FILL_ROUNDS = 3
_CONTENT_MAX_GAP_S = 2.0  # length difference under which two results get a frame comparison
_SNIPPET_CHARS = 160
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "about", "that", "this", "from", "into", "want", "watch",
    "video", "videos", "some", "any", "like", "least", "long", "minutes", "show", "find",
})

SourcesOpener = Callable[[], AbstractAsyncContextManager[list[SearchSource]]]


class ContentMatcher(Protocol):
    async def same_video(self, a: SearchResult, b: SearchResult) -> bool:
        """True when both results show the same video."""


class SearchFailed(Exception):
    """No source returned anything usable."""


@dataclass
class SearchSession:
    search_id: str
    description: str
    status: str = "running"  # running | done | failed
    phase: str | None = None  # planning | searching | ranking | comparing while running
    queries: list[str] = field(default_factory=list)
    results: list[SearchResult] = field(default_factory=list)  # shown so far
    pool: list[SearchResult] = field(default_factory=list)  # ranked, not shown yet
    backlog: list[SearchResult] = field(default_factory=list)  # found, not ranked yet
    seen: SeenIndex = field(default_factory=SeenIndex)
    pages_fetched: int = 0
    exhausted: bool = False
    ranked_by_llm: bool = True
    error: str | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def has_more(self) -> bool:
        return bool(self.pool or self.backlog) or not self.exhausted

    def snapshot(self) -> dict:
        return {
            "search_id": self.search_id,
            "description": self.description,
            "status": self.status,
            "phase": self.phase,
            "queries": self.queries,
            "results": [r.to_dict() for r in self.results],
            "has_more": self.has_more,
            "ranked_by_llm": self.ranked_by_llm,
            "error": self.error,
        }


async def plan_queries(llm, description: str) -> list[str]:
    """
    The description as written, then distinct LLM rewordings, MAX_QUERIES in
    all. The description goes first so a rewording that drifts off topic never
    replaces what the user typed.
    """
    queries = [description]
    try:
        data = await llm.execute_text(Prompts.search_queries(description))
    except Exception as exc:
        logger.warning(f"Query planning failed; searching the description as written: {exc}")
        return queries
    for query in data.get("queries") or []:
        if isinstance(query, str) and query.strip() and query.strip().lower() not in {q.lower() for q in queries}:
            queries.append(query.strip())
    return queries[:MAX_QUERIES]


def interleave(results: list[SearchResult]) -> list[SearchResult]:
    """Round-robin across sources, keeping each source's own order."""
    by_source: dict[str, list[SearchResult]] = {}
    for result in results:
        by_source.setdefault(result.source, []).append(result)
    queues = list(by_source.values())
    mixed = []
    while any(queues):
        for queue in queues:
            if queue:
                mixed.append(queue.pop(0))
    return mixed


def _terms(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 3 and w not in _STOPWORDS}


def preselect(candidates: list[SearchResult], description: str) -> list[SearchResult]:
    """
    Order candidates for the ranker: found by the most sources and queries
    first, then by how many description words the title and snippet share.
    Ties keep their order.
    """
    wanted = _terms(description)
    return sorted(candidates, key=lambda c: (-c.hits, -len(wanted & _terms(f"{c.title} {c.snippet}"))))


def _length(seconds: float | None) -> str:
    if not seconds:
        return "?"
    total = int(seconds)
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def _candidate_line(index: int, result: SearchResult) -> str:
    host = (urlparse(result.url).hostname or "").removeprefix("www.")
    parts = [str(index), result.title, _length(result.duration), host]
    if result.snippet:
        parts.append(result.snippet[:_SNIPPET_CHARS])
    return " | ".join(parts)


async def rank(llm, description: str, candidates: list[SearchResult]) -> tuple[list[SearchResult], bool]:
    """
    (matches best first, ranked_by_llm). The LLM scores every candidate and
    those at MIN_MATCH_SCORE or above are kept, ties in candidate order.
    Without a working LLM, every candidate comes back interleaved by source.
    """
    lines = [_candidate_line(i, c) for i, c in enumerate(candidates)]
    try:
        data = await llm.execute_text(Prompts.rank_search_results(description, lines))
    except Exception as exc:
        logger.warning(f"Ranking failed; interleaving sources instead: {exc}")
        return interleave(candidates), False

    scored: list[tuple[int, int, str]] = []  # (score, index, reason)
    used: set[int] = set()
    for item in data.get("scores") or []:
        if not isinstance(item, dict):
            continue
        index, score = item.get("id"), item.get("score")
        if (isinstance(index, int) and isinstance(score, int) and 0 <= index < len(candidates)
                and index not in used and score >= MIN_MATCH_SCORE):
            used.add(index)
            scored.append((score, index, str(item.get("reason") or "").strip()))
    scored.sort(key=lambda s: (-s[0], s[1]))
    return [dataclasses.replace(candidates[index], reason=reason) for _, index, reason in scored], True


class SearchAgent:
    def __init__(self, llm, open_sources: SourcesOpener, matcher: ContentMatcher | None = None) -> None:
        self._llm = llm
        self._open_sources = open_sources
        self._matcher = matcher

    async def first_page(self, session: SearchSession) -> None:
        session.phase = "planning"
        session.queries = await plan_queries(self._llm, session.description)
        await self._show_next(session)

    async def more(self, session: SearchSession) -> None:
        await self._show_next(session)

    async def _show_next(self, session: SearchSession) -> None:
        page: list[SearchResult] = []
        rounds = 0
        while len(page) < PAGE_SIZE:
            if not session.pool:
                if not session.has_more or rounds >= _MAX_FILL_ROUNDS:
                    break
                await self._fill(session)
                rounds += 1
                continue
            candidate = session.pool.pop(0)
            if not await self._shows_a_shown_video(session, candidate, session.results + page):
                page.append(candidate)
        session.results.extend(page)
        session.phase = None

    async def _shows_a_shown_video(
        self, session: SearchSession, candidate: SearchResult, shown: list[SearchResult]
    ) -> bool:
        """Compare frames with shown results of nearly the same length on other sites."""
        if self._matcher is None or candidate.duration is None:
            return False
        site = _site(candidate.url)
        for other in shown:
            if (other.duration is not None and _site(other.url) != site
                    and abs(other.duration - candidate.duration) <= _CONTENT_MAX_GAP_S):
                session.phase = "comparing"
                if await self._matcher.same_video(candidate, other):
                    logger.info(f"Skipped {candidate.url[:100]}: same video as {other.url[:100]}")
                    return True
        return False

    async def _fill(self, session: SearchSession) -> None:
        """Fetch the next page from every source and rank whatever is new."""
        fresh: list[SearchResult] = []
        if not session.exhausted:
            session.phase = "searching"
            page = session.pages_fetched + 1
            fresh = await self._fetch_page(session.queries, page)
            session.pages_fetched = page
            fresh = dedupe(fresh, seen=session.seen)
            for result in fresh:
                session.seen.add(result)
            session.exhausted = not fresh

        candidates = preselect(interleave(session.backlog + fresh), session.description)
        considered, session.backlog = candidates[:RANK_CANDIDATE_CAP], candidates[RANK_CANDIDATE_CAP:]
        if not considered:
            return
        session.phase = "ranking"
        picks, by_llm = await rank(self._llm, session.description, considered)
        session.ranked_by_llm = session.ranked_by_llm and by_llm
        session.pool.extend(picks)

    async def _fetch_page(self, queries: list[str], page: int) -> list[SearchResult]:
        async with self._open_sources() as sources:
            jobs = [(source, query) for source in sources for query in queries]
            outcomes = await asyncio.gather(
                *(source.search(query, page) for source, query in jobs), return_exceptions=True
            )
        results: list[SearchResult] = []
        failures = []
        for (source, query), outcome in zip(jobs, outcomes):
            if isinstance(outcome, BaseException):
                failures.append(f"{source.name}: {outcome}")
                logger.warning(f"Search source {source.name} failed for {query!r}: {outcome}")
            else:
                results.extend(outcome)
        if failures and len(failures) == len(jobs):
            raise SearchFailed("Every search source failed: " + "; ".join(failures)[:500])
        return results
