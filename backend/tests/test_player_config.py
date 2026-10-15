"""Tests for reading a player's stream URL from page HTML without a browser."""

import httpx
import pytest

from app.services.scraper.player_config import (
    api_media_urls,
    find_player_streams,
    player_frames,
    player_sources,
)

PAGE = "https://watch.example/v/abc123"

# eval(function(p,a,c,k,e,d){...}) packing of:
#   jwplayer("vplayer").setup({sources:[{file:"https://cdn.example/hls/abc/master.m3u8?t=x"}]})
PACKED = (
    "<script>eval(function(p,a,c,k,e,d){while(c--)if(k[c])p=p.replace(new RegExp('\\\\b'+c.toString(a)+'\\\\b','g'),k[c]);"
    "return p}('0(\"1\").2({3:[{4:\"5://6.7/8/9/a.b?t=c\"}]})',13,13,"
    "'jwplayer|vplayer|setup|sources|file|https|cdn|example|hls|abc|master|m3u8|x'.split('|')))</script>"
)


def test_player_sources_read_plain_player_configs_and_skip_non_media_urls():
    html = """<script>
      var player = videojs('p');
      player.src({src: "https:\\/\\/cdn.example\\/v\\/1080.mp4", type: "video/mp4"});
      jwplayer().setup({file: "/media/abc.m3u8", image: "https://cdn.example/poster.jpg",
                        tracks: [{file: "/srt/empty.srt"}], logo: {file: "https://cdn.example/logo.svg"}});
    </script>"""

    assert player_sources(html, PAGE) == [
        "https://cdn.example/v/1080.mp4",
        "https://watch.example/media/abc.m3u8",
    ]


def test_player_sources_unpack_packed_scripts():
    assert player_sources(PACKED, PAGE) == ["https://cdn.example/hls/abc/master.m3u8?t=x"]


def test_player_sources_drop_ad_urls_and_repeats():
    html = """<script>
      var ad = "https://ads.doubleclick.net/v/preroll.mp4";
      var a = {file: "https://cdn.example/v.m3u8"}; var b = {file: "https://cdn.example/v.m3u8"};
    </script>"""

    assert player_sources(html, PAGE) == ["https://cdn.example/v.m3u8"]


def test_player_frames_list_embedded_players_but_not_ads_or_blank_frames():
    html = """
      <iframe src="https://player.example/e/abc123"></iframe>
      <iframe data-src="//player2.example/embed/9"></iframe>
      <iframe src="about:blank"></iframe>
      <iframe src="https://ads.doubleclick.net/frame"></iframe>
    """

    assert player_frames(html, PAGE) == ["https://player.example/e/abc123", "https://player2.example/embed/9"]


def client_for(pages: dict[str, str], seen: list):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), request.headers.get("referer")))
        url = str(request.url)
        if url in pages:
            body = pages[url]
            if body.startswith("REDIRECT "):
                return httpx.Response(302, headers={"location": body.split(" ", 1)[1]})
            return httpx.Response(200, text=body, headers={"content-type": "text/html"})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_find_player_streams_reads_the_page_first():
    seen = []
    page = PACKED + '<meta itemprop="duration" content="P0DT0H18M6S" />'
    async with client_for({PAGE: page}, seen) as client:
        found = await find_player_streams(PAGE, "UA", client=client)

    assert [(s.url, s.player_page) for s in found.streams] == [("https://cdn.example/hls/abc/master.m3u8?t=x", PAGE)]
    assert found.facts.duration == 1086.0
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_find_player_streams_follow_iframes_with_a_browsers_referer():
    article = '<article><p>Story</p><iframe src="https://player.example/e/abc123"></iframe></article>'
    seen = []
    async with client_for({PAGE: article, "https://player.example/e/abc123": PACKED}, seen) as client:
        found = await find_player_streams(PAGE, "UA", client=client)

    assert [(s.url, s.player_page) for s in found.streams] == [
        ("https://cdn.example/hls/abc/master.m3u8?t=x", "https://player.example/e/abc123"),
    ]
    assert seen[1] == ("https://player.example/e/abc123", "https://watch.example/")


@pytest.mark.asyncio
async def test_find_player_streams_never_follow_redirects_into_private_networks():
    seen = []
    async with client_for({PAGE: "REDIRECT http://127.0.0.1/admin"}, seen) as client:
        assert (await find_player_streams(PAGE, "UA", client=client)).streams == []

    assert [url for url, _ in seen] == [PAGE]



# ── Pipeline stage ────────────────────────────────────────────────────────────

import os  # noqa: E402

from app.services.scraper import pipeline  # noqa: E402
from app.services.scraper.candidates import PageVideoFacts  # noqa: E402
from app.services.scraper.player_config import PlayerPage, PlayerStream  # noqa: E402

TRAILER = PlayerStream(url="https://cdn.example/hls/trailer.m3u8", player_page="https://player.example/e/abc123")
FULL = PlayerStream(url="https://cdn.example/hls/full.m3u8", player_page="https://player.example/e/abc123")


@pytest.fixture
def player_stage(monkeypatch):
    # `lengths` maps a stream URL to its length, both before (probe) and after download.
    downloads, lengths, probes = [], {}, []

    async def nothing(*args, **kwargs):
        return None

    async def download(src, referer, user_agent="", **kwargs):
        downloads.append((src, referer, user_agent))
        name = f"{src.rsplit('/', 1)[-1]}.mp4"
        with open(os.path.join(pipeline._settings.temp_storage_dir, name), "wb") as f:
            f.write(b"stub")
        return f"http://testserver/temp_storage/{name}"

    monkeypatch.setattr(pipeline, "_detect_direct_video_embed", nothing)
    monkeypatch.setattr(pipeline, "_try_ytdlp_on_page", nothing)
    monkeypatch.setattr(pipeline, "_download_embed_video", download)
    downloaded_from = {}

    def probe(url, referer, user_agent="", cookies=""):
        probes.append((url, referer, user_agent))
        return lengths[url]

    monkeypatch.setattr(pipeline, "probe_remote_duration", probe)
    monkeypatch.setattr(pipeline, "_probe_file_duration",
                        lambda path: lengths[downloaded_from[path.rsplit("/", 1)[-1]]])

    def use(streams, declared, stream_lengths):
        async def find(url, user_agent):
            return PlayerPage(streams=streams, facts=PageVideoFacts(content_urls=(), duration=declared, video_id=None))

        monkeypatch.setattr(pipeline, "find_player_streams", find)
        lengths.update(stream_lengths)
        downloaded_from.update({f"{u.rsplit('/', 1)[-1]}.mp4": u for u in stream_lengths})
        return downloads

    return use


@pytest.mark.asyncio
async def test_player_config_streams_download_without_a_browser(player_stage):
    downloads = player_stage([FULL], 1086.0, {FULL.url: 1085.0})

    result = await pipeline.run_extraction(PAGE, "UA")

    assert result[2] == [FULL.url]
    assert result[4] == "http://testserver/temp_storage/full.m3u8.mp4"
    assert downloads == [(FULL.url, "https://player.example/", "UA")]


@pytest.mark.asyncio
async def test_player_config_streams_far_shorter_than_the_page_are_never_downloaded(player_stage):
    downloads = player_stage([TRAILER, FULL], 1086.0, {TRAILER.url: 60.0, FULL.url: 1085.0})

    result = await pipeline.run_extraction(PAGE, "UA")

    assert result[4] == "http://testserver/temp_storage/full.m3u8.mp4"
    assert [src for src, _, _ in downloads] == [FULL.url]


@pytest.mark.asyncio
async def test_a_hover_preview_is_skipped_and_the_best_rendition_wins_without_a_stated_length(player_stage):
    # An embedded player lists its hover preview first, then 240p, 360p and HLS.
    page = "https://player.example/embedframe/abc"
    preview, low, mid, hls = (PlayerStream(url=f"https://cdn.example/0/{name}", player_page=page)
                              for name in ("preview.mp4", "video_240p.mp4", "video_360p.mp4", "hls.m3u8"))
    downloads = player_stage([preview, low, mid, hls], None,
                             {preview.url: 5.0, low.url: 612.0, mid.url: 612.4, hls.url: 611.9})

    result = await pipeline.run_extraction(PAGE, "UA")

    assert result[2] == [hls.url]
    assert [src for src, _, _ in downloads] == [hls.url]



# ── Player API responses ──────────────────────────────────────────────────────

API = "https://player.example/api/stream"


def test_api_responses_that_name_a_stream_are_read():
    body = ('{"filecode": "abc", "streaming_url": "https:\\/\\/cdn.example\\/hls2\\/abc\\/master.m3u8",'
            ' "thumbnail": "https://cdn.example/t.jpg", "subtitles": "None"}')

    assert api_media_urls("fetch", "application/json", body, API) == ["https://cdn.example/hls2/abc/master.m3u8"]
    assert api_media_urls("xhr", "text/plain", body, API) == ["https://cdn.example/hls2/abc/master.m3u8"]


@pytest.mark.parametrize("resource_type, content_type", [
    ("script", "application/javascript"),  # libraries, not answers
    ("document", "text/html"),
    ("fetch", "image/jpeg"),
    ("media", "video/mp4"),
])
def test_only_api_answers_are_read(resource_type, content_type):
    body = '{"file": "https://cdn.example/v.m3u8"}'

    assert api_media_urls(resource_type, content_type, body, API) == []
