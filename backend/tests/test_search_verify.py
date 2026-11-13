"""Tests for keeping only search results that lead to a video page."""

import httpx
import pytest

from app.services.search.models import SearchResult
from app.services.search.verify import VideoPageCheck, has_video_evidence

PAGE = "https://tube.example/watch/123/title"


@pytest.mark.parametrize("html", [
    '<script type="application/ld+json">{"@type": "VideoObject", "name": "x"}</script>',
    '<div itemscope itemtype="http://schema.org/VideoObject"><meta itemprop="name" content="x"></div>',
    '<meta property="og:type" content="video.other">',
    '<meta property="og:video" content="https://player.example/v/1">',
    '<meta name="twitter:player" content="https://player.example/v/1">',
    '<script>jwplayer("p").setup({file: "https://cdn.example/v/master.m3u8"})</script>',
    '<iframe src="https://player.example/embed/30163/"></iframe>',
    '<iframe src="https://www.youtube.com/embed/2pTGdH0A4Qs"></iframe>',
])
def test_pages_that_declare_or_play_a_video_count(html):
    assert has_video_evidence(f"<html><body>{html}</body></html>", PAGE) is True


@pytest.mark.parametrize("html", [
    '<article><h1>Recipe</h1><p>Boil the pasta.</p><img src="/a.jpg"></article>',  # article
    '<div class="gallery"><img src="/1.jpg"><img src="/2.jpg"></div>',  # gallery
    # A listing page: hover previews and links to video pages, but no player of its own.
    '<a href="/watch/1"><video src="/previews/1.mp4" muted loop></video></a>'
    '<a href="/watch/2"><video src="/previews/2.mp4" muted loop></video></a>',
    '<meta property="og:type" content="website"><iframe src="https://ads.example/banner"></iframe>',
])
def test_articles_galleries_and_listings_do_not(html):
    assert has_video_evidence(f"<html><body>{html}</body></html>", PAGE) is False


def result(url, source="engine", is_video=False):
    return SearchResult(url=url, title="t", source=source, is_video=is_video)


def check_with(pages: dict, fetched: list) -> VideoPageCheck:
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        fetched.append(url)
        status, content_type, body = pages[url]
        if isinstance(status, Exception):
            raise status
        return httpx.Response(status, text=body, headers={"content-type": content_type})

    return VideoPageCheck("UA", client_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))


@pytest.mark.asyncio
async def test_only_results_that_lead_to_a_video_are_kept_in_order():
    pages = {
        "https://tube.example/v/1": (200, "text/html", '<meta property="og:type" content="video">'),
        "https://blog.example/post": (200, "text/html", "<article>Words</article>"),
        "https://walled.example/v/2": (403, "text/html", "Blocked"),
        "https://gone.example/v/3": (404, "text/html", "Not found"),
        "https://slow.example/v/4": (httpx.ReadTimeout("slow"), "", ""),
        "https://cdn.example/v/5.mp4": (200, "video/mp4", ""),
        "https://docs.example/a.pdf": (200, "application/pdf", ""),
        # yt-dlp's extractor for this site matches any post, so a URL match proves nothing.
        "https://www.reddit.com/r/Cooking/comments/1abc/tips/": (200, "text/html", "<article>Tips</article>"),
    }
    fetched = []
    results = [result(u) for u in pages] + [
        result("https://www.youtube.com/watch?v=2pTGdH0A4Qs", source="youtube", is_video=True),
        result("http://127.0.0.1/admin"),
    ]

    kept = await check_with(pages, fetched).video_pages(results)

    assert [r.url for r in kept] == [
        "https://tube.example/v/1",
        "https://walled.example/v/2",  # refused: it can't be checked, so it stays
        "https://slow.example/v/4",
        "https://cdn.example/v/5.mp4",
        "https://www.youtube.com/watch?v=2pTGdH0A4Qs",
    ]
    # Results a source confirmed as videos need no fetch, and private addresses are never fetched.
    assert "https://www.youtube.com/watch?v=2pTGdH0A4Qs" not in fetched
    assert "http://127.0.0.1/admin" not in fetched
