"""Tests for the search agent: query planning, ranking and paging."""

from contextlib import asynccontextmanager

import pytest

from app.services.search.agent import (
    FOLLOW_UP_QUERIES,
    MAX_TOTAL_QUERIES,
    PAGE_SIZE,
    RANK_BATCHES,
    RANK_CANDIDATE_CAP,
    SearchAgent,
    SearchFailed,
    SearchSession,
    follow_up_queries,
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


@pytest.mark.asyncio
async def test_follow_up_queries_learn_from_matches_and_skip_tried_queries():
    llm = FakeLLM({"queries": ["Deep Sea", "Abyss Live Cam", "  ", 4, "midwater squid", "abyss live cam", "x", "y"]})

    queries = await follow_up_queries(llm, "deep sea", ["deep sea", "ocean film"],
                                      ["Abyss Live Cam: Episode 4", "Midwater Squid Hunt"], limit=5)

    assert queries == ["Abyss Live Cam", "midwater squid", "x"][:FOLLOW_UP_QUERIES]
    assert "Abyss Live Cam: Episode 4" in llm.prompts[0]
    assert "ocean film" in llm.prompts[0]


@pytest.mark.asyncio
async def test_follow_up_queries_respect_the_limit_and_survive_llm_failures():
    assert await follow_up_queries(FakeLLM({"queries": ["a", "b", "c"]}), "q", ["q"], [], limit=1) == ["a"]
    assert await follow_up_queries(FakeLLM(RuntimeError("down")), "q", ["q"], ["t"], limit=3) == []


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

    assert [(x.url, x.reason, x.score) for x in ranked] == [
        (candidates[2].url, "Exact topic", 3), (candidates[0].url, "Close", 2),
    ]
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

def videos(prefix, count, source="engine", start=0):
    return [r(f"{prefix}{n}", source) for n in range(start, start + count)]


class ScriptedLLM:
    """Answers by prompt type: planned queries, follow-up queries and scores by title."""

    def __init__(self, plan=(), follow_ups=(), scores=None, default_score=3):
        self.plan = list(plan)
        self.follow_ups = list(follow_ups)
        self.scores = scores or {}
        self.default_score = default_score
        self.prompts = []

    def kinds(self):
        return [kind for kind, _ in self.prompts]

    async def execute_text(self, prompt):
        if "You plan web searches" in prompt:
            self.prompts.append(("plan", prompt))
            return {"queries": self.plan}
        if "You refine a video search" in prompt:
            self.prompts.append(("follow_up", prompt))
            return {"queries": self.follow_ups.pop(0) if self.follow_ups else []}
        self.prompts.append(("rank", prompt))
        lines = prompt.split("Candidates:\n")[1].splitlines()
        scores = []
        for line in lines:
            index, title = line.split(" | ")[:2]
            scores.append({"id": int(index), "score": self.scores.get(title, self.default_score), "reason": "r"})
        return {"scores": scores}


def rank_lines(llm):
    return [line for kind, prompt in llm.prompts if kind == "rank" for line in prompt.split("Candidates:\n")[1].splitlines()]


@pytest.mark.asyncio
async def test_first_page_shows_a_full_page_of_deduped_results_and_skips_failing_sources():
    yt = FakeSource("youtube", {1: videos("y", 15, "youtube")})
    engine = FakeSource("engine", {1: videos("e", 15) + videos("y", 2, "youtube")})
    broken = FakeSource("bilibili", error=SourceError("HTTP 412"))
    session = SearchSession(search_id="s1", description="deep sea")

    await SearchAgent(ScriptedLLM(), sources_of(yt, engine, broken)).first_page(session)

    assert len(session.results) == PAGE_SIZE
    assert len({x.url for x in session.results}) == PAGE_SIZE
    assert session.has_more is True
    assert session.ranked_by_llm is True
    assert yt.calls[0] == ("deep sea", 1)


@pytest.mark.asyncio
async def test_results_are_ordered_by_score_then_by_how_high_and_often_they_were_found():
    # "both" sits first on two lists; "low" is last on one; "best" scores highest.
    one = FakeSource("one", {1: [r("both"), r("mid"), r("low")]})
    two = FakeSource("two", {1: [r("both"), r("best")]})
    llm = ScriptedLLM(scores={"Video best": 3, "Video both": 2, "Video mid": 2, "Video low": 2})
    session = SearchSession(search_id="s1", description="deep sea")

    await SearchAgent(llm, sources_of(one, two)).first_page(session)

    assert [x.url for x in session.results] == [r("best").url, r("both").url, r("mid").url, r("low").url]


@pytest.mark.asyncio
async def test_the_first_page_searches_again_with_queries_learned_from_its_matches():
    first = videos("a", 3)
    found_later = r("exact")
    source = FakeSource("engine")

    async def search(query, page):
        source.calls.append((query, page))
        return {"deep sea": first, "Abyss Cam Episode 4": [found_later]}.get(query, []) if page == 1 else []

    source.search = search
    llm = ScriptedLLM(follow_ups=[["Abyss Cam Episode 4"]], scores={"Video exact": 3}, default_score=2)
    session = SearchSession(search_id="s1", description="deep sea")

    await SearchAgent(llm, sources_of(source)).first_page(session)

    follow_up_prompt = next(p for kind, p in llm.prompts if kind == "follow_up")
    assert all(x.title in follow_up_prompt for x in first)
    assert ("Abyss Cam Episode 4", 1) in source.calls
    assert session.queries[-1] == "Abyss Cam Episode 4"
    assert session.results[0].url == found_later.url  # the best match leads, though found last


@pytest.mark.asyncio
async def test_follow_up_matches_compete_for_the_first_page_even_when_round_one_fills_it():
    source = FakeSource("engine")

    async def search(query, page):
        source.calls.append((query, page))
        if page != 1:
            return []
        return videos("a", PAGE_SIZE + 5) if query == "deep sea" else [r("exact")]

    source.search = search
    llm = ScriptedLLM(follow_ups=[["Abyss Cam Episode 4"]], scores={"Video exact": 3}, default_score=2)
    session = SearchSession(search_id="s1", description="deep sea")

    await SearchAgent(llm, sources_of(source)).first_page(session)

    assert ("Abyss Cam Episode 4", 1) in source.calls
    assert session.results[0].url == r("exact").url


@pytest.mark.asyncio
async def test_more_shows_a_full_page_of_net_new_results():
    source = FakeSource("engine", {page: videos("v", PAGE_SIZE, start=(page - 1) * (PAGE_SIZE - 5))
                                   for page in (1, 2, 3, 4)})
    agent = SearchAgent(ScriptedLLM(), sources_of(source))
    session = SearchSession(search_id="s1", description="deep sea")

    await agent.first_page(session)
    await agent.more(session)

    urls = [x.url for x in session.results]
    assert len(urls) == 2 * PAGE_SIZE
    assert len(set(urls)) == len(urls)


@pytest.mark.asyncio
async def test_new_queries_catch_up_before_older_queries_turn_the_page():
    source = FakeSource("engine")

    async def search(query, page):
        source.calls.append((query, page))
        return videos(f"{query}-{page}-", 3)

    source.search = search
    llm = ScriptedLLM(follow_ups=[["fresh"]])
    agent = SearchAgent(llm, sources_of(source))
    session = SearchSession(search_id="s1", description="old")

    await agent.first_page(session)

    assert source.calls[:3] == [("old", 1), ("fresh", 1), ("old", 2)]


@pytest.mark.asyncio
async def test_search_ends_when_no_query_finds_anything_new_and_the_planner_has_none():
    source = FakeSource("engine", {1: videos("v", 3), 2: videos("v", 3)})
    session = SearchSession(search_id="s1", description="deep sea")

    await SearchAgent(ScriptedLLM(), sources_of(source)).first_page(session)

    assert len(session.results) == 3
    assert session.has_more is False


@pytest.mark.asyncio
async def test_follow_up_queries_stop_at_the_query_cap():
    source = FakeSource("engine", {1: videos("v", 1)})
    llm = ScriptedLLM(follow_ups=[[f"q{n}a", f"q{n}b", f"q{n}c"] for n in range(10)])
    session = SearchSession(search_id="s1", description="deep sea")

    await SearchAgent(llm, sources_of(source)).first_page(session)

    assert len(session.queries) == MAX_TOTAL_QUERIES


@pytest.mark.asyncio
async def test_search_fails_when_every_source_fails():
    agent = SearchAgent(ScriptedLLM(), sources_of(FakeSource("a", error=SourceError("x")),
                                                  FakeSource("b", error=SourceError("y"))))

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
async def test_ranking_runs_in_capped_batches_and_the_rest_waits_in_the_backlog():
    total = RANK_CANDIDATE_CAP * RANK_BATCHES + 5
    source = FakeSource("engine", {1: videos("v", total)})
    llm = ScriptedLLM()
    session = SearchSession(search_id="s1", description="deep sea")

    await SearchAgent(llm, sources_of(source)).first_page(session)

    rank_prompts = [p for kind, p in llm.prompts if kind == "rank"]
    assert len(rank_prompts) == RANK_BATCHES
    assert all(len(p.split("Candidates:\n")[1].splitlines()) == RANK_CANDIDATE_CAP for p in rank_prompts)
    assert len(session.backlog) == 5


def test_preselect_puts_the_closest_titles_first_then_the_most_voted():
    import dataclasses

    candidates = [
        r(0, title="Cooking pasta at home"),
        dataclasses.replace(r(1, title="Humpback whales migrating north"), votes=0.1),
        dataclasses.replace(r(2, title="Ocean sounds"), votes=0.9),
        dataclasses.replace(r(3, title="Whales migrating"), votes=0.3),
        dataclasses.replace(r(4, title="Humpback whales migrating"), votes=0.5),
    ]

    ordered = preselect(candidates, "calm documentary about humpback whales migrating")

    assert [c.url for c in ordered] == [r(4).url, r(1).url, r(3).url, r(2).url, r(0).url]


class FakeVerifier:
    def __init__(self, *not_videos):
        self.not_videos = set(not_videos)
        self.checked = []

    async def video_pages(self, results):
        self.checked.extend(x.url for x in results)
        return [x for x in results if x.url not in self.not_videos]


@pytest.mark.asyncio
async def test_matches_that_do_not_lead_to_a_video_are_never_shown():
    article, gallery = r("article"), r("gallery")
    source = FakeSource("engine", {1: [article, r("v1"), gallery, r("v2")]})
    verifier = FakeVerifier(article.url, gallery.url)
    session = SearchSession(search_id="s1", description="deep sea")

    await SearchAgent(ScriptedLLM(), sources_of(source), verifier=verifier).first_page(session)

    assert [x.url for x in session.results] == [r("v1").url, r("v2").url]
    assert article.url in verifier.checked


@pytest.mark.asyncio
async def test_session_phase_follows_the_search_steps():
    phases = []

    class PhaseSpy(FakeSource):
        async def search(self, query, page):
            phases.append(session.phase)
            return await super().search(query, page)

    class PhaseLLM(ScriptedLLM):
        async def execute_text(self, prompt):
            phases.append(session.phase)
            return await super().execute_text(prompt)

    class PhaseVerifier(FakeVerifier):
        async def video_pages(self, results):
            phases.append(session.phase)
            return await super().video_pages(results)

    session = SearchSession(search_id="s1", description="deep sea")

    await SearchAgent(PhaseLLM(), sources_of(PhaseSpy("youtube", {1: [r(0)]})),
                      verifier=PhaseVerifier()).first_page(session)

    assert phases[:5] == ["planning", "searching", "ranking", "verifying", "refining"]
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
    source = FakeSource("engine", {1: [shown, copy, other, same_site]})
    matcher = FakeMatcher((shown.url, copy.url))
    session = SearchSession(search_id="s1", description="deep sea")

    await SearchAgent(ScriptedLLM(), sources_of(source), matcher=matcher).first_page(session)

    urls = [x.url for x in session.results]
    assert urls == [shown.url, other.url, same_site.url]
    # Only results on another site within a couple of seconds are compared.
    assert matcher.calls == [(copy.url, shown.url)]


@pytest.mark.asyncio
async def test_content_checks_also_cover_results_shown_on_earlier_pages():
    first = [r(n, "site-a", duration=100.0 + n * 10) for n in range(PAGE_SIZE)]
    copy = r(900, "site-b", title="Retitled copy", duration=101.0)
    later = r(901, "site-c", duration=9000.0)
    source = FakeSource("engine", {1: first, 2: [copy, later]})
    matcher = FakeMatcher((first[0].url, copy.url))
    agent = SearchAgent(ScriptedLLM(), sources_of(source), matcher=matcher)
    session = SearchSession(search_id="s1", description="deep sea")

    await agent.first_page(session)
    await agent.more(session)

    assert [x.url for x in session.results][PAGE_SIZE:] == [later.url]
