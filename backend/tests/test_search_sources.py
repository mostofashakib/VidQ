"""Tests for the yt-dlp and browser-engine search sources."""

import json

import pytest

from app.config import Settings
from app.services.search.sources import (
    BrowserEngineSource,
    SourceError,
    YtDlpSource,
    brave_videos_url,
    duckduckgo_videos_url,
)


def test_safe_search_is_off_by_default_and_rejects_unknown_levels(monkeypatch):
    monkeypatch.delenv("SEARCH_SAFE_SEARCH", raising=False)
    assert Settings().search_safe_search == "off"

    monkeypatch.setenv("SEARCH_SAFE_SEARCH", "Strict")
    assert Settings().search_safe_search == "strict"

    monkeypatch.setenv("SEARCH_SAFE_SEARCH", "loose")
    assert Settings().search_safe_search == "off"


# ── yt-dlp search ─────────────────────────────────────────────────────────────

def entry(n):
    return json.dumps({
        "url": f"https://www.youtube.com/watch?v=vid{n:08d}",
        "webpage_url": f"https://www.youtube.com/watch?v=vid{n:08d}",
        "title": f"Video {n}",
        "duration": 60.0 * n,
        "thumbnails": [{"url": "https://i.example/small.jpg"}, {"url": f"https://i.example/{n}.jpg"}],
        "description": f"About video {n}",
        "channel": "Some Channel",
    })


@pytest.mark.asyncio
async def test_ytdlp_source_maps_entries_and_pages_by_slicing():
    commands = []

    async def runner(args):
        commands.append(args)
        return "\n".join(entry(n) for n in range(1, 7)) + "\n"

    source = YtDlpSource("youtube", "ytsearch", per_page=3, runner=runner)

    page_two = await source.search("ocean film", page=2)

    assert commands[0][-1] == "ytsearch6:ocean film"
    assert "--flat-playlist" in commands[0] and "-j" in commands[0]
    assert [r.title for r in page_two] == ["Video 4", "Video 5", "Video 6"]
    first = page_two[0]
    assert first.url == "https://www.youtube.com/watch?v=vid00000004"
    assert first.duration == 240.0
    assert first.thumbnail == "https://i.example/4.jpg"
    assert first.snippet == "About video 4"
    assert first.source == "youtube"


@pytest.mark.asyncio
async def test_ytdlp_source_skips_malformed_lines_and_reports_failures():
    async def partly_broken(args):
        return "not json\n" + entry(1) + "\n" + json.dumps({"title": "no url"}) + "\n"

    results = await YtDlpSource("youtube", "ytsearch", per_page=5, runner=partly_broken).search("q", page=1)
    assert [r.title for r in results] == ["Video 1"]

    async def failing(args):
        raise SourceError("HTTP Error 412")

    with pytest.raises(SourceError):
        await YtDlpSource("bilibili", "bilisearch", runner=failing).search("q", page=1)


# ── Browser engines ───────────────────────────────────────────────────────────

def test_engine_urls_carry_the_safe_search_level_and_page():
    assert duckduckgo_videos_url("deep sea", "off", 1) == \
        "https://duckduckgo.com/?q=deep+sea&iax=videos&ia=videos&kp=-2"
    assert duckduckgo_videos_url("deep sea", "strict", 1).endswith("&kp=1")
    assert brave_videos_url("deep sea", "moderate", 1) == \
        "https://search.brave.com/videos?q=deep+sea&safesearch=moderate"
    assert brave_videos_url("deep sea", "off", 3) == \
        "https://search.brave.com/videos?q=deep+sea&safesearch=off&offset=2"


@pytest.mark.asyncio
async def test_browser_source_scrolls_for_later_pages_and_harvests_links():
    fetches = []

    class FakeFetcher:
        async def fetch(self, url, scrolls):
            fetches.append((url, scrolls))
            return """<body>
              <div><a href="https://videos.example.org/v/1">Deep sea film</a> 10:00</div>
              <div><a href="https://apps.example/get-our-app">Get the app</a></div>
            </body>"""

    source = BrowserEngineSource("duckduckgo", duckduckgo_videos_url, safe_search="off",
                                 fetcher=FakeFetcher(), scroll_paging=True)

    results = await source.search("deep sea", page=3)

    assert fetches == [("https://duckduckgo.com/?q=deep+sea&iax=videos&ia=videos&kp=-2", 2)]
    assert [(r.url, r.title, r.duration, r.source) for r in results] == [
        ("https://videos.example.org/v/1", "Deep sea film", 600.0, "duckduckgo")
    ]


@pytest.mark.asyncio
async def test_url_paged_engines_load_the_page_url_without_scrolling():
    fetches = []

    class FakeFetcher:
        async def fetch(self, url, scrolls):
            fetches.append((url, scrolls))
            return "<body></body>"

    await BrowserEngineSource("brave", brave_videos_url, safe_search="off", fetcher=FakeFetcher()).search("q", page=2)

    assert fetches == [("https://search.brave.com/videos?q=q&safesearch=off&offset=1", 0)]
