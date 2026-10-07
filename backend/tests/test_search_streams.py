"""Tests for resolving a result page to a playable stream, and the stream registry."""

import json

import pytest

from app.services.search.playback import StreamRegistry, rewrite_playlist
from app.services.search.sources import SourceError
from app.services.search.streams import (
    STREAM_FORMAT,
    ResolvedStream,
    StreamUnavailable,
    parse_ytdlp_cookies,
    resolve_stream,
    stream_from_info,
)

COOKIES = (
    "a=1; Domain=.site.example; Path=/; Expires=1791428283; "
    "sid=xyz; Domain=.site.example; Path=/; Secure; "
    "other=2; Domain=cdn.other.example; Path=/"
)


def info(**overrides):
    base = {
        "url": "https://cdn.site.example/v/1080p.mp4",
        "protocol": "https",
        "duration": 620,
        "http_headers": {
            "User-Agent": "UA/1", "Accept": "text/html", "Sec-Fetch-Mode": "navigate",
            "Referer": "https://site.example/v/1/",
        },
        "cookies": COOKIES,
    }
    base.update(overrides)
    return base


# ── Cookies and headers ───────────────────────────────────────────────────────

def test_parse_ytdlp_cookies_reads_name_value_and_domain():
    assert parse_ytdlp_cookies(COOKIES) == (
        ("a", "1", "site.example"), ("sid", "xyz", "site.example"), ("other", "2", "cdn.other.example"),
    )
    assert parse_ytdlp_cookies("") == ()


def test_request_headers_send_each_cookie_only_to_its_domain():
    stream = stream_from_info(info())

    assert stream.request_headers("https://cdn.site.example/seg1.ts") == {
        "User-Agent": "UA/1", "Referer": "https://site.example/v/1/", "Cookie": "a=1; sid=xyz",
    }
    assert "Cookie" not in stream.request_headers("https://elsewhere.example/x")


def test_stream_from_info_detects_hls_and_keeps_the_length():
    assert stream_from_info(info()).kind == "file"
    assert stream_from_info(info(protocol="m3u8_native")).kind == "hls"
    assert stream_from_info(info()).duration == 620.0
    assert stream_from_info(info(duration=None)).duration is None


# ── Resolving ─────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_resolve_stream_asks_ytdlp_for_one_browser_playable_format():
    calls = []

    async def runner(args):
        calls.append(args)
        return json.dumps(info())

    stream = await resolve_stream("https://site.example/v/1", runner=runner)

    assert stream.url == "https://cdn.site.example/v/1080p.mp4"
    assert calls[0][-1] == "https://site.example/v/1"
    assert ["-f", STREAM_FORMAT] == calls[0][calls[0].index("-f"):calls[0].index("-f") + 2]
    assert "--no-playlist" in calls[0]


@pytest.mark.asyncio
async def test_resolve_stream_reports_unsupported_pages():
    async def failing(args):
        raise SourceError("Unsupported URL")

    async def empty(args):
        return ""

    with pytest.raises(StreamUnavailable, match="Unsupported URL"):
        await resolve_stream("https://site.example/v/1", runner=failing)
    with pytest.raises(StreamUnavailable):
        await resolve_stream("https://site.example/v/1", runner=empty)


# ── Registry ──────────────────────────────────────────────────────────────────

def stream(url="https://cdn.site.example/master.m3u8"):
    return ResolvedStream(url=url, kind="hls", headers={}, cookies=(), duration=None)


def test_registry_hands_out_unguessable_ids_and_expires_them(monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr("app.services.search.playback.time.time", lambda: clock[0])
    registry = StreamRegistry(ttl_s=60)

    stream_id = registry.register(stream())

    assert len(stream_id) == 32
    assert registry.get(stream_id).stream.url == "https://cdn.site.example/master.m3u8"
    clock[0] += 61
    assert registry.get(stream_id) is None


def test_registry_only_allows_the_stream_url_and_urls_it_was_told_about():
    registry = StreamRegistry()
    entry = registry.get(registry.register(stream()))

    assert entry.allows("https://cdn.site.example/master.m3u8")
    assert not entry.allows("https://cdn.site.example/seg1.ts")
    entry.allow(["https://cdn.site.example/seg1.ts"])
    assert entry.allows("https://cdn.site.example/seg1.ts")


# ── HLS playlists ─────────────────────────────────────────────────────────────

def test_rewrite_playlist_routes_every_uri_through_the_proxy():
    playlist = "\n".join([
        "#EXTM3U",
        '#EXT-X-KEY:METHOD=AES-128,URI="key.bin"',
        "#EXTINF:4.0,",
        "seg1.ts",
        "#EXTINF:4.0,",
        "https://other.example/abs/seg2.ts",
        "",
    ])

    text, urls = rewrite_playlist(playlist, "https://cdn.site.example/hls/index.m3u8", lambda u: f"P[{u}]")

    assert text.splitlines() == [
        "#EXTM3U",
        '#EXT-X-KEY:METHOD=AES-128,URI="P[https://cdn.site.example/hls/key.bin]"',
        "#EXTINF:4.0,",
        "P[https://cdn.site.example/hls/seg1.ts]",
        "#EXTINF:4.0,",
        "P[https://other.example/abs/seg2.ts]",
    ]
    assert urls == [
        "https://cdn.site.example/hls/key.bin",
        "https://cdn.site.example/hls/seg1.ts",
        "https://other.example/abs/seg2.ts",
    ]


@pytest.mark.asyncio
async def test_unavailable_streams_carry_ytdlps_reason_without_its_boilerplate():
    async def failing(args):
        raise SourceError("ERROR: [Site] abc: Unable to extract mpd_url; please report this issue on  https://x , "
                          "filling out the appropriate issue template.")

    with pytest.raises(StreamUnavailable) as caught:
        await resolve_stream("https://site.example/v/1", runner=failing)

    assert str(caught.value) == "[Site] abc: Unable to extract mpd_url"
