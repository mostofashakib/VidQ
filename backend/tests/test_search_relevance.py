"""Tests for ordering search results by relevance and position votes."""

import dataclasses

from app.services.search.models import SearchResult
from app.services.search.relevance import VOTE_K, by_relevance, position_votes


def r(n, score=0, votes=0.0):
    return SearchResult(url=f"https://v.example/{n}", title=f"Video {n}", source="engine", score=score, votes=votes)


def test_each_list_votes_for_its_results_less_the_lower_they_sit():
    voted = position_votes([r(0), r(1), r(2)])

    assert [x.votes for x in voted] == [1 / (VOTE_K + 1), 1 / (VOTE_K + 2), 1 / (VOTE_K + 3)]
    assert [x.url for x in voted] == [r(0).url, r(1).url, r(2).url]


def test_the_llm_score_decides_first_and_votes_order_results_within_a_score():
    results = [r(0, score=2, votes=0.9), r(1, score=3, votes=0.1), r(2, score=3, votes=0.4), r(3, score=2, votes=0.9)]

    assert [x.url for x in by_relevance(results)] == [r(2).url, r(1).url, r(0).url, r(3).url]


def test_unscored_results_follow_their_votes():
    results = [r(0, votes=0.1), r(1, votes=0.3), dataclasses.replace(r(2), votes=0.2)]

    assert [x.url for x in by_relevance(results)] == [r(1).url, r(2).url, r(0).url]
