"""Tests for picking the page's main video out of every media URL it loads."""

import pytest
from bs4 import BeautifulSoup

from app.services.scraper.candidates import (
    Candidate,
    PageVideoFacts,
    choose_candidates,
    VideoElement,
    extract_page_facts,
    has_trailer_cue,
    is_acceptable_download,
    main_element,
    parse_video_elements,
    rank_candidates,
    vet_candidates,
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


# ── Trailers ──────────────────────────────────────────────────────────────────



def test_trailer_cues_come_from_path_label_and_container_words():
    assert has_trailer_cue("https://v.example/videos/90563_trailer.mp4")
    assert has_trailer_cue("https://v.example/clip/90563.mp4")
    assert has_trailer_cue("https://v.example/v.mp4", label="Watch Trailer")
    assert has_trailer_cue("https://v.example/v.mp4", container="video-teaser-box")
    assert not has_trailer_cue("https://v.example/clips/90563_1080p.mp4")
    assert not has_trailer_cue("https://v.example/v.mp4", label="HD", container="player video-js")


def test_ranking_marks_trailer_suspects_from_element_labels():
    facts = PageVideoFacts(content_urls=(), duration=None, video_id="90563")
    trailer = VideoElement(src="https://v.example/90563_a.mp4", area=10, duration=None, muted=False,
                           loop=False, controls=True, container="player", label="Trailer")

    ranked, _ = rank_candidates([MAIN, trailer.src], [trailer], facts)

    by_url = {c.url: c for c in ranked}
    assert by_url[trailer.src].trailer_suspected is True
    assert by_url[MAIN].trailer_suspected is False


def cand(url, trailer=False, score=40):
    return Candidate(url=url, score=score, trusted=True, reasons=(), trailer_suspected=trailer)


def test_vetting_skips_files_much_shorter_than_the_declared_length():
    full, short = cand("https://v.example/full.mp4"), cand("https://v.example/short.mp4", score=100)

    result = vet_candidates([short, full], {short.url: 60.0, full.url: 845.0}, declared_s=845.0)

    assert [c.url for c in result.kept] == [full.url]
    assert result.skipped == [(short.url, "60s file but the page declares ~845s")]
    assert result.only_trailers is False


def test_vetting_reports_when_every_candidate_is_a_trailer():
    short = cand("https://v.example/short.mp4")

    result = vet_candidates([short], {short.url: 60.0}, declared_s=845.0)

    assert result.kept == []
    assert result.only_trailers is True


def test_named_trailers_are_dropped_only_when_another_candidate_remains():
    named, plain = cand("https://v.example/t.mp4", trailer=True), cand("https://v.example/p.mp4")

    both = vet_candidates([named, plain], {}, declared_s=None)
    alone = vet_candidates([named], {}, declared_s=None)

    assert [c.url for c in both.kept] == [plain.url]
    assert both.skipped == [(named.url, "named like a trailer")]
    assert [c.url for c in alone.kept] == [named.url]
    assert alone.only_trailers is False


def test_vetting_prefers_the_longest_probed_file_and_keeps_unprobed_ones_last():
    a, b, c = cand("https://v.example/a.mp4", score=90), cand("https://v.example/b.mp4"), cand("https://v.example/c.mp4")

    result = vet_candidates([a, b, c], {a.url: 600.0, b.url: 845.0, c.url: None}, declared_s=None)

    assert [x.url for x in result.kept] == [b.url, a.url, c.url]



# ── Declared videos and length evidence ───────────────────────────────────────

MICRODATA = """
<article itemscope itemtype="https://schema.org/VideoObject">
  <span itemprop="author" itemscope itemtype="https://schema.org/Person">
    <meta itemprop="contentUrl" content="https://people.example/avatar.mp4" />
  </span>
  <meta itemprop="duration" content="P0DT0H20M18S" />
  <meta itemprop="contentUrl" content="https://media.example/videos/Some%20Title_converted.m3u8" />
</article>
<div itemscope itemtype="https://schema.org/ImageObject">
  <meta itemprop="contentUrl" content="https://site.example/poster.png" />
</div>
"""


def test_page_facts_read_microdata_video_objects_scoped_to_the_item():
    facts = extract_page_facts(BeautifulSoup(MICRODATA, "html.parser"), "https://site.example/some-title/")

    assert facts.content_urls == ("https://media.example/videos/Some%20Title_converted.m3u8",)
    assert facts.duration == 1218.0


@pytest.mark.parametrize("meta, expected", [
    ('<meta property="og:video:secure_url" content="https://media.example/v/1080.mp4">',
     ("https://media.example/v/1080.mp4",)),
    ('<meta property="og:video" content="https://media.example/v/master.m3u8">',
     ("https://media.example/v/master.m3u8",)),
    ('<meta property="og:video:url" content="https://media.example/play?id=7">'
     '<meta property="og:video:type" content="video/mp4">',
     ("https://media.example/play?id=7",)),
    ('<meta name="twitter:player:stream" content="https://media.example/v/7.mp4">',
     ("https://media.example/v/7.mp4",)),
    # An embed player page is not the media file itself.
    ('<meta property="og:video" content="https://site.example/embed/7">'
     '<meta property="og:video:type" content="text/html">', ()),
])
def test_page_facts_read_open_graph_and_twitter_video_files(meta, expected):
    facts = extract_page_facts(BeautifulSoup(f"<head>{meta}</head>", "html.parser"), "https://site.example/p/")

    assert facts.content_urls == expected


def test_declared_urls_rank_as_trusted_page_evidence():
    facts = PageVideoFacts(content_urls=("https://media.example/videos/a.m3u8",), duration=1218.0, video_id=None)

    ranked, _ = rank_candidates(["https://media.example/videos/a.m3u8"], [], facts)

    assert ranked[0].trusted is True
    assert "matches the page's declared video" in ranked[0].reasons


def test_a_file_matching_the_declared_length_is_trusted_over_a_wrongly_trusted_ad():
    # A pre-roll plays in the main player, so its URL looks trusted; the real
    # stream has no other evidence but its length matches the page.
    ad = Candidate(url="https://ads.example/preroll.mp4", score=60, trusted=True, reasons=("main player element",))
    real = Candidate(url="https://media.example/v.m3u8", score=0, trusted=False, reasons=())
    other = Candidate(url="https://media.example/related.mp4", score=0, trusted=False, reasons=())
    probed = {ad.url: 2.0, real.url: 1217.6, other.url: 300.0}

    chosen = choose_candidates([ad, real, other], probed, declared_s=1218.0)
    vetting = vet_candidates(chosen, probed, declared_s=1218.0)

    assert [c.url for c in chosen] == [ad.url, real.url]
    assert chosen[1].trusted and "length matches the page's ~1218s" in chosen[1].reasons
    assert [c.url for c in vetting.kept] == [real.url]


def test_without_a_declared_length_only_page_evidence_decides_trust():
    trusted = Candidate(url="https://media.example/a.mp4", score=40, trusted=True, reasons=("x",))
    loose = Candidate(url="https://media.example/b.mp4", score=0, trusted=False, reasons=())

    assert choose_candidates([trusted, loose], {}, declared_s=None) == [trusted]
    assert choose_candidates([loose], {}, declared_s=None) == [loose]


@pytest.mark.parametrize("raw, seconds", [
    ("P0DT0H20M18S", 1218.0), ("P1DT2H", 93600.0), ("PT14M5S", 845.0), ("PT1H", 3600.0),
])
def test_iso_8601_durations_with_a_day_part_are_read(raw, seconds):
    from app.services.scraper.candidates import _parse_duration_seconds

    assert _parse_duration_seconds(raw) == seconds


# ── Vetting without a declared length ────────────────────────────────────────

def untrusted(url):
    return Candidate(url=url, score=0, trusted=False, reasons=())


def test_without_a_declared_length_files_far_shorter_than_the_longest_are_skipped():
    # A hover preview or pre-roll sits next to the real file and is far shorter.
    teaser, full = untrusted("https://cdn.example/0/teaser_a.mp4"), untrusted("https://cdn.example/0/main_a.mp4")

    result = vet_candidates([teaser, full], {teaser.url: 5.0, full.url: 612.0}, declared_s=None)

    assert [c.url for c in result.kept] == [full.url]
    assert result.skipped == [(teaser.url, "5s file next to a 612s one")]
    assert result.only_trailers is False


def test_equal_length_files_prefer_an_adaptive_stream_then_the_highest_resolution():
    low, mid, hls = (untrusted("https://cdn.example/v/video_240p.mp4"), untrusted("https://cdn.example/v/video_360p.mp4"),
                     untrusted("https://cdn.example/v/hls.m3u8"))
    hd = untrusted("https://cdn.example/v/video_1080p.mp4")

    with_hls = vet_candidates([low, mid, hls], {low.url: 612.0, mid.url: 612.4, hls.url: 611.9}, declared_s=None)
    files_only = vet_candidates([low, hd, mid], {low.url: 612.0, hd.url: 611.8, mid.url: 612.4}, declared_s=None)

    assert [c.url for c in with_hls.kept] == [hls.url, mid.url, low.url]
    assert [c.url for c in files_only.kept] == [hd.url, mid.url, low.url]
