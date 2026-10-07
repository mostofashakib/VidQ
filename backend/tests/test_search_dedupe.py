"""Tests for collapsing duplicate search results across sources and mirrors."""

from app.services.search.dedupe import SeenIndex, canonical_url, dedupe, video_key
from app.services.search.models import SearchResult


def result(url, title="Ocean Documentary Full", duration=None, thumbnail="", source="a"):
    return SearchResult(url=url, title=title, source=source, duration=duration, thumbnail=thumbnail)


# ── Canonical URLs ────────────────────────────────────────────────────────────

def test_canonical_url_drops_tracking_and_normalizes_host():
    assert canonical_url("https://WWW.Example.com/watch/123/?utm_source=x&b=2&fbclid=z&a=1#t=30") == \
        "https://example.com/watch/123?a=1&b=2"
    assert canonical_url("http://m.example.com/v/9") == "http://example.com/v/9"


def test_canonical_url_unwraps_search_engine_redirects():
    wrapped = "https://duckduckgo.com/l/?uddg=https%3A%2F%2Fvideos.example.org%2Fv%2F42%3Futm_medium%3Dx&rut=abc"

    assert canonical_url(wrapped) == "https://videos.example.org/v/42"
    assert canonical_url("https://www.google.com/url?q=https://example.com/v/1&sa=U") == "https://example.com/v/1"


def test_canonical_url_keeps_the_site_root_and_rejects_non_http():
    assert canonical_url("https://example.com/") == "https://example.com/"
    assert canonical_url("javascript:void(0)") is None
    assert canonical_url("/relative/path") is None


# ── Video identity ────────────────────────────────────────────────────────────

def test_video_key_maps_every_url_form_of_one_video_to_one_key():
    keys = {
        video_key("https://www.youtube.com/watch?v=2pTGdH0A4Qs&t=10"),
        video_key("https://youtu.be/2pTGdH0A4Qs"),
        video_key("https://www.youtube.com/shorts/2pTGdH0A4Qs"),
        video_key("https://m.youtube.com/watch?v=2pTGdH0A4Qs"),
    }

    assert len(keys) == 1
    assert keys.pop() is not None


def test_video_key_is_none_for_pages_no_extractor_recognises():
    assert video_key("https://unknown-videos.example/watch/77") is None


# ── Dedupe ────────────────────────────────────────────────────────────────────

def test_dedupe_collapses_url_forms_and_fills_missing_fields():
    items = [
        result("https://www.youtube.com/watch?v=2pTGdH0A4Qs", source="engine"),
        result("https://youtu.be/2pTGdH0A4Qs", duration=4366.0, thumbnail="https://i.example/t.jpg", source="ytsearch"),
        result("https://example.com/v/1?utm_source=x", title="Other video"),
        result("https://example.com/v/1", title="Other video"),
    ]

    kept = dedupe(items)

    assert [r.url for r in kept] == ["https://www.youtube.com/watch?v=2pTGdH0A4Qs", "https://example.com/v/1?utm_source=x"]
    assert kept[0].duration == 4366.0
    assert kept[0].thumbnail == "https://i.example/t.jpg"
    assert kept[0].source == "engine"


def test_dedupe_treats_same_title_and_length_on_another_site_as_a_mirror():
    original = result("https://site-a.example/v/1", title="Deep Sea Creatures: The Full Story!", duration=600.0)
    mirror = result("https://site-b.example/watch/xyz", title="deep sea creatures the full story", duration=602.0)
    other_cut = result("https://site-c.example/v/9", title="Deep Sea Creatures: The Full Story!", duration=300.0)

    assert [r.url for r in dedupe([original, mirror, other_cut])] == [original.url, other_cut.url]


def test_short_or_untimed_titles_are_never_treated_as_mirrors():
    a = result("https://site-a.example/v/1", title="Ocean", duration=60.0)
    b = result("https://site-b.example/v/2", title="Ocean", duration=60.0)
    c = result("https://site-c.example/v/3", title="Deep Sea Creatures Full Story")
    d = result("https://site-d.example/v/4", title="Deep Sea Creatures Full Story")

    assert len(dedupe([a, b, c, d])) == 4


def test_dedupe_skips_results_already_seen_in_earlier_pages():
    seen = SeenIndex()
    for r in dedupe([result("https://youtu.be/2pTGdH0A4Qs")]):
        seen.add(r)

    fresh = dedupe([result("https://www.youtube.com/watch?v=2pTGdH0A4Qs"), result("https://example.com/v/new")],
                   seen=seen)

    assert [r.url for r in fresh] == ["https://example.com/v/new"]


def test_dedupe_counts_how_often_each_video_was_found():
    items = [
        result("https://youtu.be/2pTGdH0A4Qs", source="ytsearch"),
        result("https://www.youtube.com/watch?v=2pTGdH0A4Qs", source="engine"),
        result("https://www.youtube.com/shorts/2pTGdH0A4Qs", source="engine"),
        result("https://example.com/v/1", title="Other video"),
    ]

    assert [r.hits for r in dedupe(items)] == [3, 1]


def test_same_title_uploads_on_one_site_are_different_videos_not_mirrors():
    # A channel that reuses one title for several uploads of similar length.
    first = result("https://www.youtube.com/watch?v=EJ3VbyjKAsc", title="Ultimate Funny Cat Compilation", duration=915.0)
    second = result("https://www.youtube.com/watch?v=H-EX1Qz5nGc", title="Ultimate Funny Cat Compilation", duration=917.0)

    assert len(dedupe([first, second])) == 2


def test_mirrors_retitled_on_another_site_are_one_video():
    # One upload copied to two sites, each with its own wording of the title.
    first = result("https://site-a.example/v/r8Mvk90zRsc",
                   title="Siblings Try Anal Seductive Blonde Sindee and Scott, uploaded by Rieneeretina", duration=620.0)
    copy = result("https://site-b.example/v/MYP5-ve40b0",
                  title="Siblings get dirty anal sindee and scott, LollipopBoobs", duration=620.0)

    assert [r.url for r in dedupe([first, copy])] == [first.url]


def test_retitled_mirrors_across_pages_are_skipped():
    seen = SeenIndex()
    seen.add(result("https://site-a.example/v/1", title="Humpback whales migrating north at dawn", duration=900.0))

    fresh = dedupe([result("https://site-b.example/v/2", title="Whales migrating north: humpback dawn footage",
                           duration=901.0)], seen=seen)

    assert fresh == []


def test_titles_sharing_few_words_or_lengths_apart_are_different_videos():
    base = result("https://site-a.example/v/1", title="Humpback whales migrating north at dawn", duration=900.0)
    loose = result("https://site-b.example/v/2", title="Whales of the arctic sea, a calm film", duration=900.0)
    other_length = result("https://site-c.example/v/3", title="Humpback whales migrating north at dawn part two",
                          duration=700.0)

    assert len(dedupe([base, loose, other_length])) == 3
