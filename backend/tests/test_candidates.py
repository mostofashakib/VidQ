"""Tests for picking the page's main video out of every media URL it loads."""

import pytest
from bs4 import BeautifulSoup

from app.services.scraper.candidates import (
    PageVideoFacts,
    VideoElement,
    extract_page_facts,
    is_acceptable_download,
    main_element,
    parse_video_elements,
    rank_candidates,
)

PAGE = "https://site.example/video/90563/some-title/?__vs=1"
MAIN = "https://media.example/vfile/90563/90172/8/aaa/1791289580/mp4/90563_1080p.mp4"
AD = "https://cdn-worker.adhost.example/video/09d81225/480p.mp4"
PROMO = "https://site.example/templates/images/native/4206.mp4"
PREVIEW = "https://cdn.site.example/media/videos/tmb/000/090/563/preview.mp4"

LD_JSON = """
<script type="application/ld+json">
{"@context": "https://schema.org", "@type": "VideoObject",
 "contentUrl": "https://media.example/vfile/90563/90172/8/zzz/1791289690/mp4/90563_1080p.mp4",
 "duration": "PT14M5S"}
</script>
"""


def element(src, area=1000, container="", muted=False, loop=False, controls=True, duration=None):
    return VideoElement(
        src=src, area=area, duration=duration, muted=muted, loop=loop,
        controls=controls, container=container,
    )


PAGE_ELEMENTS = [
    element(MAIN, area=1064 * 600, container="video-js vjs-paused player player-container", controls=False),
    element(PROMO, area=261 * 156, container="thumb-wrapper thumbnail promo-card video-card-grid related",
            muted=True, loop=True, controls=False, duration=5.0),
    element(AD, area=390 * 220, container="d0S8IjIe_video_content_wrapper msg_wrapper exo_wrapper",
            muted=True, controls=False, duration=26.5),
]


# ── Page facts ────────────────────────────────────────────────────────────────

def test_page_facts_read_json_ld_video_object():
    facts = extract_page_facts(BeautifulSoup(LD_JSON, "html.parser"), PAGE)

    assert facts.duration == 845.0
    assert facts.content_urls == (
        "https://media.example/vfile/90563/90172/8/zzz/1791289690/mp4/90563_1080p.mp4",
    )
    assert facts.video_id == "90563"


def test_page_facts_find_video_objects_inside_graphs_and_lists():
    html = """<script type="application/ld+json">
    {"@graph": [{"@type": "WebPage"}, {"@type": ["VideoObject"], "contentUrl": "https://m.example/a.mp4",
      "duration": "PT1H2M"}]}
    </script>"""

    facts = extract_page_facts(BeautifulSoup(html, "html.parser"), "https://m.example/watch")

    assert facts.content_urls == ("https://m.example/a.mp4",)
    assert facts.duration == 3720.0
    assert facts.video_id is None


def test_page_facts_fall_back_to_meta_duration_and_survive_broken_json():
    html = """<script type="application/ld+json">{not json</script>
    <meta property="video:duration" content="482">"""

    facts = extract_page_facts(BeautifulSoup(html, "html.parser"), "https://s.example/36885/slug/")

    assert facts.duration == 482.0
    assert facts.content_urls == ()
    assert facts.video_id == "36885"


# ── Ranking ───────────────────────────────────────────────────────────────────

def test_main_player_matching_json_ld_ranks_first_and_ads_are_rejected():
    facts = extract_page_facts(BeautifulSoup(LD_JSON, "html.parser"), PAGE)

    ranked, rejected = rank_candidates([AD, PROMO, PREVIEW, MAIN], PAGE_ELEMENTS, facts)

    assert ranked[0].url == MAIN
    assert ranked[0].trusted is True
    assert {url for url, _ in rejected} == {AD, PROMO}
    assert [c.url for c in ranked] == [MAIN, PREVIEW]
    assert ranked[1].trusted is False


def test_video_id_in_url_makes_a_network_only_candidate_trusted():
    facts = PageVideoFacts(content_urls=(), duration=None, video_id="36885")
    stream = "https://cdn.example/get_file/36/abc/36000/36885/36885_480p.mp4?br=452"

    ranked, _ = rank_candidates([AD, stream], [], facts)

    assert ranked[0].url == stream
    assert ranked[0].trusted is True
    assert ranked[1].trusted is False


def test_video_id_must_match_a_whole_number_not_a_fragment():
    facts = PageVideoFacts(content_urls=(), duration=None, video_id="9056")

    ranked, _ = rank_candidates([MAIN], [], facts)

    assert ranked[0].trusted is False


def test_largest_non_ad_element_is_the_main_player():
    facts = PageVideoFacts(content_urls=(), duration=None, video_id=None)
    big_ad = element(AD, area=5000, container="preroll-ad-container")
    player = element(MAIN, area=4000, container="player")

    ranked, rejected = rank_candidates([AD, MAIN], [big_ad, player], facts)

    assert [c.url for c in ranked] == [MAIN]
    assert ranked[0].trusted is True
    assert rejected == [(AD, "inside ad container")]


def test_ad_markers_match_whole_words_only():
    facts = PageVideoFacts(content_urls=(), duration=None, video_id=None)
    player = element(MAIN, area=4000, container="download-area header loaded")

    ranked, rejected = rank_candidates([MAIN], [player], facts)

    assert [c.url for c in ranked] == [MAIN]
    assert rejected == []


def test_known_ad_network_urls_are_rejected_without_element_evidence():
    facts = PageVideoFacts(content_urls=(), duration=None, video_id=None)
    ad_url = "https://syndication.exosrv.com/vast/clip.mp4"

    ranked, rejected = rank_candidates([ad_url], [], facts)

    assert ranked == []
    assert rejected == [(ad_url, "known ad URL")]


def test_relative_and_duplicate_urls_are_handled():
    facts = PageVideoFacts(content_urls=(), duration=None, video_id="90563")

    ranked, _ = rank_candidates([MAIN, MAIN, "", "blob:https://x/1"], [], facts)

    assert [c.url for c in ranked] == [MAIN]


def test_main_element_skips_ads_and_handles_empty_pages():
    assert main_element(PAGE_ELEMENTS).src == MAIN
    assert main_element([PAGE_ELEMENTS[2]]) is None
    assert main_element([]) is None


# ── Download acceptance ───────────────────────────────────────────────────────

def test_download_shorter_than_half_the_expected_length_is_rejected():
    ok, reason = is_acceptable_download(trusted=True, downloaded_s=26.0, expected_s=845.0)

    assert ok is False
    assert "expected" in reason
    assert is_acceptable_download(trusted=True, downloaded_s=840.0, expected_s=845.0) == (True, "")


def test_unknown_length_rejects_ad_length_clips_from_untrusted_sources_only():
    ok, reason = is_acceptable_download(trusted=False, downloaded_s=23.5, expected_s=None)

    assert ok is False
    assert "ad" in reason
    assert is_acceptable_download(trusted=True, downloaded_s=23.5, expected_s=None) == (True, "")
    assert is_acceptable_download(trusted=False, downloaded_s=600.0, expected_s=None) == (True, "")


def test_unreadable_download_length_is_accepted_only_from_trusted_sources():
    assert is_acceptable_download(trusted=True, downloaded_s=None, expected_s=845.0) == (True, "")
    assert is_acceptable_download(trusted=False, downloaded_s=None, expected_s=None)[0] is False


# ── Reading elements from the browser ─────────────────────────────────────────

def raw(src, area, container="", index=0, duration=None):
    return {"index": index, "src": src, "area": area, "duration": duration,
            "muted": False, "loop": False, "controls": True, "container": container}


def test_parse_video_elements_resolves_relative_sources_and_keeps_every_player():
    elements = parse_video_elements(
        [raw("/media/a.mp4", 10, index=0), raw("blob:https://site.example/x", 99, "player", index=1),
         raw("", 5, index=2)],
        PAGE,
    )

    assert [(e.src, e.index) for e in elements] == [
        ("https://site.example/media/a.mp4", 0),
        ("blob:https://site.example/x", 1),
        ("", 2),
    ]


@pytest.mark.asyncio
async def test_agent_pass_selects_the_largest_non_ad_player():
    from app.services.scraper.playback import _get_main_video_selector

    class FakePage:
        url = PAGE

        async def evaluate(self, script):
            return [
                raw(AD, 900_000, "preroll-ad-wrapper", index=0),
                raw("blob:https://site.example/x", 600_000, "video-js player", index=1),
                raw(PROMO, 40_000, "promo-card", index=2),
            ]

    assert await _get_main_video_selector(FakePage()) == 'video[data-vidq-index="1"]'
