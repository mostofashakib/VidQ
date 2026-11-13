"""
The search agent. It searches in rounds, the way a research assistant reads
results and searches again. For a description of the video a user wants:

1. The LLM plans a few search queries.
2. A round runs the queries that are furthest behind (new queries start at
   page 1 and catch up before older ones turn the page) on every source in
   parallel. Failing sources are skipped. A query that finds nothing new is
   dropped.
3. Results are deduplicated against each other and against everything seen
   before. Each result list votes for its results by position, and copies
   pool their votes (see relevance.py).
4. The candidates closest to the description go to the LLM in batches of
   RANK_CANDIDATE_CAP. It scores each one, and the rest wait in a backlog.
   Local models slow down sharply past about 20 candidates per call.
5. Matches that do not lead to a video page are dropped (see verify.py), and
   the pool is kept in relevance order: LLM score, then votes.
6. The LLM reads the best matches so far and writes follow-up queries with
   the names and wording they use. The first page always does this once, and
   later rounds do it whenever the pool cannot fill a page, up to
   MAX_TOTAL_QUERIES queries in all.
7. Results are shown PAGE_SIZE at a time, net new each time. Before a result
   is shown, it is compared by its frames with every shown result of nearly
   the same length on another site, since sites retitle copies.
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
from app.services.search.relevance import by_relevance, position_votes
from app.services.search.sources import SearchSource

logger = logging.getLogger("VideoSearch")

PAGE_SIZE = 20
MAX_QUERIES = 5  # planned up front
MAX_TOTAL_QUERIES = 12  # planned plus follow-up queries
FOLLOW_UP_QUERIES = 3  # per follow-up step
MIN_MATCH_SCORE = 2  # of 3: "likely matches"
RANK_CANDIDATE_CAP = 20  # candidates per ranking call
RANK_BATCHES = 3  # ranking calls per round; the rest wait in the backlog
_MAX_FILL_ROUNDS = 8  # search rounds per page, a bound on a single step's time
_FOLLOW_UP_EXAMPLES = 10  # matching titles shown to the follow-up planner
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


class VideoVerifier(Protocol):
    async def video_pages(self, results: list[SearchResult]) -> list[SearchResult]:
        """The results that lead to a video, in their order."""


class SearchFailed(Exception):
    """No source returned anything usable."""


@dataclass
class SearchSession:
    search_id: str
    description: str
    status: str = "running"  # running | done | failed
    phase: str | None = None  # planning | searching | ranking | verifying | refining | comparing
    queries: list[str] = field(default_factory=list)  # every query planned so far
    query_pages: dict[str, int] = field(default_factory=dict)  # live queries and their next page
    results: list[SearchResult] = field(default_factory=list)  # shown so far
    pool: list[SearchResult] = field(default_factory=list)  # ranked, not shown yet
    backlog: list[SearchResult] = field(default_factory=list)  # found, not ranked yet
    seen: SeenIndex = field(default_factory=SeenIndex)
    exhausted: bool = False  # the planner has no new queries left
    ranked_by_llm: bool = True
    error: str | None = None
    created_at: float = field(default_factory=time.time)

    @property
    def can_search(self) -> bool:
        return bool(self.backlog or self.query_pages)

    @property
    def has_more(self) -> bool:
        return bool(self.pool) or self.can_search or not self.exhausted

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


def _new_queries(data: dict, known: list[str], limit: int) -> list[str]:
    """Distinct non-empty queries from an LLM answer that are not in known, at most limit."""
    seen = {q.lower() for q in known}
    new = []
    for query in data.get("queries") or []:
        if isinstance(query, str) and query.strip() and query.strip().lower() not in seen:
            seen.add(query.strip().lower())
            new.append(query.strip())
    return new[:max(limit, 0)]


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
    return queries + _new_queries(data, queries, MAX_QUERIES - 1)


async def follow_up_queries(llm, description: str, tried: list[str], match_titles: list[str], limit: int) -> list[str]:
    """New queries that reuse the wording of the matches so far, at most limit."""
    count = min(limit, FOLLOW_UP_QUERIES)
    if count <= 0:
        return []
    try:
        data = await llm.execute_text(Prompts.follow_up_search_queries(description, tried, match_titles, count))
    except Exception as exc:
        logger.warning(f"Follow-up query planning failed: {exc}")
        return []
    return _new_queries(data, tried, count)


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
    Order candidates for the ranker: most description words shared by the
    title and snippet first, then most votes. Ties keep their order.
    """
    wanted = _terms(description)
    return sorted(candidates, key=lambda c: (-len(wanted & _terms(f"{c.title} {c.snippet}")), -c.votes))


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
    return [dataclasses.replace(candidates[index], reason=reason, score=score) for score, index, reason in scored], True


class SearchAgent:
    def __init__(
        self,
        llm,
        open_sources: SourcesOpener,
        matcher: ContentMatcher | None = None,
        verifier: VideoVerifier | None = None,
    ) -> None:
        self._llm = llm
        self._open_sources = open_sources
        self._matcher = matcher
        self._verifier = verifier

    async def first_page(self, session: SearchSession) -> None:
        session.phase = "planning"
        session.queries = await plan_queries(self._llm, session.description)
        session.query_pages = {q: 1 for q in session.queries}
        # One round, then search again with what it found, before showing anything.
        await self._fill(session)
        if await self._follow_up(session):
            await self._fill(session)
        await self._show_next(session)

    async def more(self, session: SearchSession) -> None:
        await self._show_next(session)

    async def _show_next(self, session: SearchSession) -> None:
        """Show up to PAGE_SIZE net new results, searching until the pool can fill the page."""
        page: list[SearchResult] = []
        rounds = 0
        while len(page) < PAGE_SIZE:
            needed = PAGE_SIZE - len(page)
            if len(session.pool) < needed and rounds < _MAX_FILL_ROUNDS:
                if not session.can_search:
                    await self._follow_up(session)
                if session.can_search:
                    await self._fill(session)
                    rounds += 1
                    if len(session.pool) < needed:
                        await self._follow_up(session)
                    continue
            if not session.pool:
                break
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
        """One round: search, rank what is new, keep the video pages in relevance order."""
        fresh = await self._search_round(session) if session.query_pages else []
        candidates = preselect(interleave(session.backlog + fresh), session.description)
        cap = RANK_CANDIDATE_CAP * RANK_BATCHES
        considered, session.backlog = candidates[:cap], candidates[cap:]
        if not considered:
            return
        picks = await self._rank(session, considered)
        if self._verifier and picks:
            session.phase = "verifying"
            picks = await self._verifier.video_pages(picks)
        session.pool = by_relevance(session.pool + picks)

    async def _rank(self, session: SearchSession, candidates: list[SearchResult]) -> list[SearchResult]:
        session.phase = "ranking"
        batches = [candidates[i:i + RANK_CANDIDATE_CAP] for i in range(0, len(candidates), RANK_CANDIDATE_CAP)]
        outcomes = await asyncio.gather(*(rank(self._llm, session.description, b) for b in batches))
        session.ranked_by_llm = session.ranked_by_llm and all(by_llm for _, by_llm in outcomes)
        return [pick for picks, _ in outcomes for pick in picks]

    async def _follow_up(self, session: SearchSession) -> bool:
        """
        Plan follow-up queries from the best matches so far. True when it
        added any; the planner is marked done when it has none.
        """
        if session.exhausted:
            return False
        session.phase = "refining"
        matches = [r.title for r in (session.results + session.pool)[:_FOLLOW_UP_EXAMPLES]]
        new = await follow_up_queries(
            self._llm, session.description, session.queries, matches, MAX_TOTAL_QUERIES - len(session.queries)
        )
        if not new:
            session.exhausted = True
            return False
        logger.info(f"Follow-up queries: {new}")
        session.queries.extend(new)
        session.query_pages.update({q: 1 for q in new})
        return True

    async def _search_round(self, session: SearchSession) -> list[SearchResult]:
        """Run the live queries furthest behind; drop those that find nothing new."""
        session.phase = "searching"
        lowest = min(session.query_pages.values())
        jobs = [(q, page) for q, page in session.query_pages.items() if page == lowest]
        found = await self._fetch(jobs)
        for query, results in found.items():
            if any(session.seen.find(r) is None for r in results):
                session.query_pages[query] += 1
            else:
                del session.query_pages[query]
        fresh = dedupe([r for results in found.values() for r in results], seen=session.seen)
        for result in fresh:
            session.seen.add(result)
        return fresh

    async def _fetch(self, jobs: list[tuple[str, int]]) -> dict[str, list[SearchResult]]:
        """Each query's results from every source, voted by position within each list."""
        async with self._open_sources() as sources:
            calls = [(source, query, page) for source in sources for query, page in jobs]
            outcomes = await asyncio.gather(
                *(source.search(query, page) for source, query, page in calls), return_exceptions=True
            )
        found: dict[str, list[SearchResult]] = {query: [] for query, _ in jobs}
        failures = []
        for (source, query, _), outcome in zip(calls, outcomes):
            if isinstance(outcome, BaseException):
                failures.append(f"{source.name}: {outcome}")
                logger.warning(f"Search source {source.name} failed for {query!r}: {outcome}")
            else:
                found[query].extend(position_votes(outcome))
        if failures and len(failures) == len(calls):
            raise SearchFailed("Every search source failed: " + "; ".join(failures)[:500])
        return found
