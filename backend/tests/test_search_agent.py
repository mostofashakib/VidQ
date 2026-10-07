"""Tests for the search agent: query planning, ranking and paging."""

from contextlib import asynccontextmanager

import pytest

from app.services.search.agent import (
    PAGE_SIZE,
    RANK_CANDIDATE_CAP,
    SearchAgent,
    SearchFailed,
    SearchSession,
    interleave,
    plan_queries,
    preselect,
    rank,
)
from app.services.prompts import Prompts
from app.services.search.models import SearchResult
from app.services.search.sources import SourceError


def r(n, source="youtube", title=None, duration=None):
    return SearchResult(url=f"https://{source}.example/v/{n}", title=title or f"Video {n}", source=source,
                        duration=duration)


class FakeLLM:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    async def execute_text(self, prompt):
        self.prompts.append(prompt)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeSource:
    def __init__(self, name, pages=None, error=None):
        self.name = name
        self.pages = pages or {}
        self.error = error
        self.calls = []

    async def search(self, query, page):
        self.calls.append((query, page))
        if self.error:
            raise self.error
        return self.pages.get(page, [])


def sources_of(*sources):
    @asynccontextmanager
    async def open_sources():
        yield list(sources)
    return open_sources


# ── Planning ──────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_plan_queries_cleans_and_caps_the_llm_queries():
    llm = FakeLLM({"queries": ["deep sea film", "Deep Sea Film", "  ", 7, "abyss documentary",
                               "ocean creatures", "midwater", "trench video", "extra"]})

    queries = await plan_queries(llm, "a film about deep sea creatures")

    assert queries == ["a film about deep sea creatures", "deep sea film", "abyss documentary", "ocean creatures",
                       "midwater"]
    assert "a film about deep sea creatures" in llm.prompts[0]


@pytest.mark.asyncio
async def test_plan_queries_always_searches_the_description_as_written_first():
    llm = FakeLLM({"queries": ["Scott and Sindee documentary", "scott and sindee brother and sister"]})

    queries = await plan_queries(llm, "scott and sindee brother and sister")

    assert queries == ["scott and sindee brother and sister", "Scott and Sindee documentary"]


def test_query_prompt_keeps_the_users_terms_and_adds_no_genre():
    prompt = Prompts.search_queries("skateboard tricks fail")

    assert "documentary" not in prompt.lower()
    assert "Do not add a genre" in prompt


def test_ranking_prompt_scores_every_candidate_and_requires_the_names():
    prompt = Prompts.rank_search_results("skateboard tricks fail", ["0 | Skate fails | 1:00 | x.com"])

    assert "Score every candidate" in prompt
    assert "every name" in prompt


@pytest.mark.asyncio
async def test_plan_queries_falls_back_to_the_description():
    assert await plan_queries(FakeLLM(RuntimeError("down")), "deep sea") == ["deep sea"]
    assert await plan_queries(FakeLLM({"queries": []}), "deep sea") == ["deep sea"]


# ── Ranking ───────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_rank_keeps_matching_scores_best_first_with_reasons():
    candidates = [r(0, duration=600.0), r(1), r(2), r(3)]
    llm = FakeLLM({"scores": [
        {"id": 0, "score": 2, "reason": "Close"}, {"id": 1, "score": 1, "reason": "Loose"},
        {"id": 2, "score": 3, "reason": "Exact topic"}, {"id": 3, "score": 0},
        {"id": 2, "score": 3, "reason": "dup"}, {"id": 9, "score": 3, "reason": "bad id"},
        {"id": 3, "score": "3", "reason": "bad score"},
    ]})

    ranked, by_llm = await rank(llm, "deep sea", candidates)

    assert [(x.url, x.reason) for x in ranked] == [(candidates[2].url, "Exact topic"), (candidates[0].url, "Close")]
    assert by_llm is True
    assert "0 | Video 0 | 10:00 | youtube.example" in llm.prompts[0]


@pytest.mark.asyncio
async def test_rank_falls_back_to_interleaving_sources_when_the_llm_fails():
    candidates = [r(0, "a"), r(1, "a"), r(2, "b"), r(3, "a")]

    ranked, by_llm = await rank(FakeLLM(RuntimeError("down")), "q", candidates)

    assert [x.url for x in ranked] == [x.url for x in interleave(candidates)]
    assert [x.source for x in ranked] == ["a", "b", "a", "a"]
    assert by_llm is False


# ── Sessions and paging ───────────────────────────────────────────────────────

def all_ids(n):
    return {"scores": [{"id": i, "score": 3, "reason": f"r{i}"} for i in range(n)]}


@pytest.mark.asyncio
async def test_first_page_shows_five_deduped_results_and_skips_failing_sources():
    yt = FakeSource("youtube", {1: [r(n) for n in range(4)]})
    engine = FakeSource("engine", {1: [r(n, "engine") for n in range(4)] + [r(0)]})
    broken = FakeSource("bilibili", error=SourceError("HTTP 412"))
    llm = FakeLLM({"queries": ["deep sea"]}, all_ids(8))
    session = SearchSession(search_id="s1", description="deep sea")

    await SearchAgent(llm, sources_of(yt, engine, broken)).first_page(session)

    assert len(session.results) == PAGE_SIZE
    assert len({x.url for x in session.results}) == PAGE_SIZE
    assert len(session.pool) == 3
    assert session.has_more is True
    assert session.ranked_by_llm is True
    assert session.queries == ["deep sea"]
    assert yt.calls == [("deep sea", 1)]


@pytest.mark.asyncio
async def test_more_serves_the_pool_before_fetching_new_pages():
    yt = FakeSource("youtube", {1: [r(n) for n in range(7)], 2: [r(n) for n in range(5, 12)]})
    llm = FakeLLM({"queries": ["deep sea"]}, all_ids(7), all_ids(5))
    agent = SearchAgent(llm, sources_of(yt))
    session = SearchSession(search_id="s1", description="deep sea")

    await agent.first_page(session)
    await agent.more(session)

    assert [x.url for x in session.results] == [r(n).url for n in range(10)]
    assert yt.calls == [("deep sea", 1), ("deep sea", 2)]
    assert "5 |" not in llm.prompts[2].split("Candidates:")[1]  # page 2 ranks only unseen results


@pytest.mark.asyncio
async def test_search_is_exhausted_when_sources_return_nothing_new():
    yt = FakeSource("youtube", {1: [r(n) for n in range(3)], 2: [r(n) for n in range(3)]})
    llm = FakeLLM({"queries": ["deep sea"]}, all_ids(3))
    agent = SearchAgent(llm, sources_of(yt))
    session = SearchSession(search_id="s1", description="deep sea")

    await agent.first_page(session)

    assert len(session.results) == 3
    assert session.has_more is False


@pytest.mark.asyncio
async def test_search_fails_when_every_source_fails():
    llm = FakeLLM({"queries": ["deep sea"]})
    agent = SearchAgent(llm, sources_of(FakeSource("a", error=SourceError("x")), FakeSource("b", error=SourceError("y"))))

    with pytest.raises(SearchFailed, match="Every search source failed"):
        await agent.first_page(SearchSession(search_id="s1", description="deep sea"))


def test_session_snapshot_lists_shown_results():
    session = SearchSession(search_id="s1", description="deep sea")
    session.results = [r(1)]

    snapshot = session.snapshot()

    assert snapshot["search_id"] == "s1"
    assert snapshot["status"] == "running"
    assert snapshot["results"][0]["url"] == r(1).url
    assert snapshot["has_more"] is True


@pytest.mark.asyncio
async def test_candidates_over_the_ranking_cap_wait_for_the_next_page():
    total = RANK_CANDIDATE_CAP + 5
    yt = FakeSource("youtube", {1: [r(n) for n in range(total)]})
    llm = FakeLLM({"queries": ["deep sea"]}, all_ids(5), all_ids(5))
    agent = SearchAgent(llm, sources_of(yt))
    session = SearchSession(search_id="s1", description="deep sea")

    await agent.first_page(session)

    assert len(llm.prompts[1].split("Candidates:\n")[1].splitlines()) == RANK_CANDIDATE_CAP
    assert len(session.backlog) == 5

    await agent.more(session)

    second_rank_lines = llm.prompts[2].split("Candidates:\n")[1].splitlines()
    assert [line.split(" | ")[1] for line in second_rank_lines] == [f"Video {n}" for n in range(RANK_CANDIDATE_CAP, total)]
    assert session.has_more is False


def test_preselect_puts_videos_found_most_often_and_closest_to_the_description_first():
    import dataclasses

    candidates = [
        r(0, title="Cooking pasta at home"),
        r(1, title="Humpback whales migrating north"),
        dataclasses.replace(r(2, title="Ocean sounds"), hits=3),
        r(3, title="Whales of the deep"),
    ]

    ordered = preselect(candidates, "calm documentary about humpback whales migrating")

    assert [c.url for c in ordered] == [r(2).url, r(1).url, r(3).url, r(0).url]


@pytest.mark.asyncio
async def test_session_phase_follows_the_search_steps():
    phases = []

    class PhaseSpy(FakeSource):
        async def search(self, query, page):
            phases.append(session.phase)
            return await super().search(query, page)

    class RankSpyLLM(FakeLLM):
        async def execute_text(self, prompt):
            phases.append(session.phase)
            return await super().execute_text(prompt)

    session = SearchSession(search_id="s1", description="deep sea")
    llm = RankSpyLLM({"queries": ["deep sea"]}, all_ids(1))

    await SearchAgent(llm, sources_of(PhaseSpy("youtube", {1: [r(0)]}))).first_page(session)

    assert phases[:3] == ["planning", "searching", "ranking"]
    assert session.snapshot()["phase"] is None

# ── Content duplicates ────────────────────────────────────────────────────────

class FakeMatcher:
    def __init__(self, *duplicate_pairs):
        self.duplicates = {frozenset(pair) for pair in duplicate_pairs}
        self.calls = []

    async def same_video(self, a, b):
        self.calls.append((a.url, b.url))
        return frozenset((a.url, b.url)) in self.duplicates


@pytest.mark.asyncio
async def test_results_showing_the_same_video_as_an_earlier_one_are_skipped():
    shown = r(0, "site-a", title="One title", duration=620.0)
    copy = r(1, "site-b", title="Completely different words", duration=621.0)
    other = r(2, "site-c", title="Another", duration=300.0)
    same_site = r(3, "site-a", title="Same site upload", duration=620.0)
    fresh = [r(n, "site-d", duration=50.0 + n) for n in range(10, 13)]
    source = FakeSource("engine", {1: [shown, copy, other, same_site, *fresh]})
    matcher = FakeMatcher((shown.url, copy.url))
    llm = FakeLLM({"queries": ["deep sea"]}, all_ids(7))
    session = SearchSession(search_id="s1", description="deep sea")

    await SearchAgent(llm, sources_of(source), matcher=matcher).first_page(session)

    urls = [x.url for x in session.results]
    assert copy.url not in urls
    assert len(urls) == PAGE_SIZE
    assert {shown.url, other.url, same_site.url} <= set(urls)
    # Only results on another site within a couple of seconds are compared.
    assert matcher.calls == [(copy.url, shown.url)]


@pytest.mark.asyncio
async def test_content_checks_also_cover_results_shown_on_earlier_pages():
    first = [r(n, "site-a", duration=100.0 + n * 10) for n in range(5)]
    copy = r(9, "site-b", title="Retitled copy", duration=101.0)
    later = r(8, "site-c", duration=900.0)
    source = FakeSource("engine", {1: first, 2: [copy, later]})
    matcher = FakeMatcher((first[0].url, copy.url))
    llm = FakeLLM({"queries": ["deep sea"]}, all_ids(5), all_ids(2))
    agent = SearchAgent(llm, sources_of(source), matcher=matcher)
    session = SearchSession(search_id="s1", description="deep sea")

    await agent.first_page(session)
    await agent.more(session)

    assert [x.url for x in session.results][5:] == [later.url]
