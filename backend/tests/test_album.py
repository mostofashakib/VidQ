"""Tests for expanding a multi-video album page into one download per video."""

import httpx

from app.services.scraper.album import AlbumItem, fetch_album, parse_album

PAGE = "https://albums.example/a/AbC123"


def player(src, poster="", res=None, extra="", wrapper="media-group"):
    res_attr = f" res='{res}'" if res else ""
    return f"""
    <div class="{wrapper}">
      <div class="video-lg"><video class="video-js" controls loop preload="none" poster="{poster}" {extra}>
        <source src="{src}" type="video/mp4"{res_attr}></video></div>
      <div class="video"><video class="player video-js" controls preload="none" poster="{poster}" {extra}>
        <source src="{src}" type="video/mp4"{res_attr}></video></div>
    </div>"""


def page(*bodies, title="Beach Trip"):
    return f"""<html><head><meta property="og:title" content="{title}">
    <title>{title} - Videos</title></head><body>{''.join(bodies)}</body></html>"""


def test_album_lists_each_video_once_with_poster_and_numbered_title():
    html = page(
        player("https://v1.cdn.example/1/AbC123/one_720p.mp4", "https://s1.cdn.example/one.jpg"),
        player("https://v1.cdn.example/1/AbC123/two_720p.mp4", "https://s1.cdn.example/two.jpg"),
        player("https://v1.cdn.example/1/AbC123/three_720p.mp4", "https://s1.cdn.example/three.jpg"),
    )

    album = parse_album(html, PAGE)

    assert album.title == "Beach Trip"
    assert album.items == [
        AlbumItem(url="https://v1.cdn.example/1/AbC123/one_720p.mp4",
                  thumbnail="https://s1.cdn.example/one.jpg", title="Beach Trip (1/3)"),
        AlbumItem(url="https://v1.cdn.example/1/AbC123/two_720p.mp4",
                  thumbnail="https://s1.cdn.example/two.jpg", title="Beach Trip (2/3)"),
        AlbumItem(url="https://v1.cdn.example/1/AbC123/three_720p.mp4",
                  thumbnail="https://s1.cdn.example/three.jpg", title="Beach Trip (3/3)"),
    ]


def test_a_single_video_page_is_a_one_video_album_without_numbering():
    album = parse_album(page(player("https://v.example/one.mp4", "https://s.example/one.jpg")), PAGE)

    assert album.items == [AlbumItem(url="https://v.example/one.mp4", thumbnail="https://s.example/one.jpg",
                                     title="Beach Trip")]
    assert parse_album(page("<p>No videos here</p>"), PAGE) is None


def test_trailer_named_players_are_dropped_when_a_plain_one_remains():
    html = page(
        '<video controls><source src="https://v.example/full_1080p.mp4" label="HD"></video>',
        '<video controls title="Watch the trailer"><source src="https://v.example/a.mp4"></video>',
        player("https://v.example/videos/b_teaser.mp4"),
    )

    album = parse_album(html, PAGE)

    assert [(i.url, i.title) for i in album.items] == [("https://v.example/full_1080p.mp4", "Beach Trip")]


def test_a_page_with_only_a_trailer_named_player_keeps_it():
    album = parse_album(page(player("https://v.example/sample.mp4")), PAGE)

    assert [i.url for i in album.items] == ["https://v.example/sample.mp4"]


def test_declared_length_comes_from_the_page():
    html = page(player("https://v.example/one.mp4")).replace(
        "</head>", '<script type="application/ld+json">{"@type": "VideoObject", "duration": "PT14M5S"}</script></head>')

    assert parse_album(html, PAGE).declared_duration == 845.0


def test_each_player_contributes_its_highest_resolution_source():
    html = page(
        """<video controls><source src="/media/a_480p.mp4" res="480">
           <source src="/media/a_1080p.mp4" res="1080"></video>""",
        player("https://v.example/b.mp4"),
    )

    album = parse_album(html, PAGE)

    assert [item.url for item in album.items] == [
        "https://albums.example/media/a_1080p.mp4",
        "https://v.example/b.mp4",
    ]


def test_ads_previews_and_non_file_sources_are_not_album_videos():
    html = page(
        player("https://v.example/one.mp4"),
        player("https://v.example/two.mp4"),
        player("https://v.example/slider.mp4", wrapper="msg_wrapper exo_wrapper"),
        '<div class="thumb"><video muted loop autoplay src="https://v.example/hover.mp4"></video></div>',
        '<video controls src="https://syndication.exosrv.com/clip.mp4"></video>',
        '<video controls src="blob:https://albums.example/x"></video>',
        '<video controls src="https://v.example/stream"></video>',
    )

    album = parse_album(html, PAGE)

    assert [item.url for item in album.items] == ["https://v.example/one.mp4", "https://v.example/two.mp4"]


def test_title_falls_back_to_the_page_title_then_the_url():
    bare = """<html><head><title>Road Trip</title></head><body>{}</body></html>""".format(
        player("https://v.example/one.mp4") + player("https://v.example/two.mp4"))
    assert parse_album(bare, PAGE).title == "Road Trip"

    untitled = "<html><body>{}</body></html>".format(
        player("https://v.example/one.mp4") + player("https://v.example/two.mp4"))
    assert parse_album(untitled, PAGE).title == PAGE


# ── Fetching ──────────────────────────────────────────────────────────────────

ALBUM_HTML = page(player("https://v.example/one.mp4"), player("https://v.example/two.mp4"))
DECLARED = '<script type="application/ld+json">{"@type": "VideoObject", "duration": "PT14M5S"}</script></head>'


def client_for(handler, html=None):
    if html is not None:
        return httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, text=html, headers={"content-type": "text/html"})))
    return httpx.Client(transport=httpx.MockTransport(handler))


def no_probe(url, referer, user_agent=""):
    raise AssertionError("albums without a declared length need no probe")


def test_multi_video_album_without_declared_length_is_not_probed():
    album = fetch_album(PAGE, "UA", client=client_for(None, ALBUM_HTML), probe=no_probe)

    assert len(album.items) == 2


def test_single_video_must_be_reachable_with_the_page_as_referer():
    html = page(player("https://v.example/one.mp4"))
    probes = []

    def reachable(url, referer, user_agent=""):
        probes.append((url, referer, user_agent))
        return 678.0

    assert fetch_album(PAGE, "UA", client=client_for(None, html), probe=reachable).items[0].title == "Beach Trip"
    assert probes == [("https://v.example/one.mp4", PAGE, "UA")]
    assert fetch_album(PAGE, "UA", client=client_for(None, html), probe=lambda *a, **k: None) is None


def test_files_far_shorter_than_the_declared_length_are_dropped_as_trailers():
    html = page(player("https://v.example/full.mp4"), player("https://v.example/short.mp4")).replace("</head>", DECLARED)
    lengths = {"https://v.example/full.mp4": 845.0, "https://v.example/short.mp4": 60.0}

    album = fetch_album(PAGE, "UA", client=client_for(None, html), probe=lambda url, *a, **k: lengths[url])

    assert [(i.url, i.title) for i in album.items] == [("https://v.example/full.mp4", "Beach Trip")]


def test_a_page_offering_only_trailers_falls_back_to_the_regular_pipeline():
    html = page(player("https://v.example/short.mp4")).replace("</head>", DECLARED)

    assert fetch_album(PAGE, "UA", client=client_for(None, html), probe=lambda *a, **k: 60.0) is None


def test_fetch_album_reads_the_page_with_the_given_user_agent():
    seen = {}

    def handler(request):
        seen["ua"] = request.headers["user-agent"]
        return httpx.Response(200, text=ALBUM_HTML, headers={"content-type": "text/html"})

    album = fetch_album(PAGE, "UA/1.0", client=client_for(handler))

    assert [item.url for item in album.items] == ["https://v.example/one.mp4", "https://v.example/two.mp4"]
    assert seen["ua"] == "UA/1.0"


def test_fetch_album_follows_safe_redirects_and_uses_the_final_page_as_base():
    def handler(request):
        if request.url.scheme == "http":
            return httpx.Response(301, headers={"location": PAGE})
        return httpx.Response(200, text=ALBUM_HTML, headers={"content-type": "text/html"})

    album = fetch_album("http://albums.example/a/AbC123", "UA", client=client_for(handler))

    assert album.page_url == PAGE


def test_fetch_album_refuses_redirects_into_private_networks():
    def handler(request):
        if request.url.host == "albums.example":
            return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/"})
        raise AssertionError("must not request the private address")

    assert fetch_album(PAGE, "UA", client=client_for(handler)) is None


def test_fetch_album_returns_none_for_unsafe_urls_errors_and_non_html():
    def boom(request):
        raise AssertionError("must not be requested")

    assert fetch_album("http://10.0.0.5/a/x", "UA", client=client_for(boom)) is None
    assert fetch_album(PAGE, "UA", client=client_for(lambda r: httpx.Response(403))) is None
    assert fetch_album(PAGE, "UA", client=client_for(
        lambda r: httpx.Response(200, content=b"\x00", headers={"content-type": "video/mp4"}))) is None

    def network_error(request):
        raise httpx.ConnectError("down")

    assert fetch_album(PAGE, "UA", client=client_for(network_error)) is None
