"""Unit tests for ad URL filtering and video URL utilities."""
import asyncio
import os
from types import SimpleNamespace

import pytest

from app.services.scraper import media
from app.services.scraper.media import (
    _convert_to_mp4,
    _download_budget_s,
    _download_video_direct,
    _ffmpeg_cookies,
    _get_main_playing_video_url,
    _is_ad_video_url,
    _is_forbidden,
    probe_remote_duration,
    _validate_video_file,
    browser_referer,
)


# ── Ad domain filtering ───────────────────────────────────────────────────────

def test_blocks_tsyndicate_domain():
    assert _is_ad_video_url("https://svacdn.tsyndicate.com/ad/video.mp4") is True


def test_blocks_doubleclick():
    assert _is_ad_video_url("https://ad.doubleclick.net/video.mp4") is True


def test_blocks_googlesyndication():
    assert _is_ad_video_url("https://pagead2.googlesyndication.com/video.mp4") is True


def test_blocks_adnxs():
    assert _is_ad_video_url("https://cdn.adnxs.com/video.mp4") is True


# ── Ad dimension pattern filtering ───────────────────────────────────────────

def test_blocks_440x250_dimension():
    assert _is_ad_video_url("https://cdn.example.com/ads/440x250.mp4") is True


def test_blocks_320x240_dimension():
    assert _is_ad_video_url("https://cdn.example.com/ads/320x240.mp4") is True


def test_blocks_160x90_dimension():
    assert _is_ad_video_url("https://cdn.example.com/preroll/160x90.mp4") is True


# ── Main video URLs should not be blocked ─────────────────────────────────────

def test_allows_generic_cdn():
    assert _is_ad_video_url("https://cdn.example.com/videos/main_video.mp4") is False


def test_allows_4k_resolution_in_path():
    # 1920 and 1080 are 4-digit numbers — regex only matches 2-3 digit dimensions
    assert _is_ad_video_url("https://cdn.example.com/1920x1080/video.mp4") is False


def test_allows_temp_storage_url():
    assert _is_ad_video_url("http://localhost:8000/temp_storage/abc123.mp4") is False


def test_allows_subdomain_not_in_blocklist():
    assert _is_ad_video_url("https://video.example.com/content/clip.mp4") is False


def test_handles_malformed_url_gracefully():
    assert _is_ad_video_url("not-a-url") is False
    assert _is_ad_video_url("") is False


def test_durationless_webm_with_video_stream_is_valid_for_recording(tmp_path, monkeypatch):
    from app.services.scraper import media

    webm_path = tmp_path / "recording.webm"
    webm_path.write_bytes(b"webm-video-data" * 5000)

    monkeypatch.setattr(media.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(
        media.subprocess,
        "run",
        lambda *_, **__: SimpleNamespace(
            stderr="Input #0, matroska,webm\nStream #0:0: Video: vp8, yuv420p, 1280x720",
            returncode=0,
        ),
    )

    assert _validate_video_file(str(webm_path), label="WebM recording", require_duration=False) is True
    assert _validate_video_file(str(webm_path), label="MP4", require_duration=True) is False


def test_convert_to_mp4_accepts_durationless_mediarecorder_webm(tmp_path, monkeypatch):
    from app.services.scraper import media

    webm_path = tmp_path / "recording.webm"
    mp4_path = tmp_path / "recording.mp4"
    webm_path.write_bytes(b"webm-video-data" * 5000)
    calls = []

    def fake_run(cmd, capture_output=True, text=True, timeout=None):
        calls.append(cmd)
        if "-y" in cmd and cmd[-1] == str(mp4_path):
            mp4_path.write_bytes(b"mp4-video-data" * 5000)
            return SimpleNamespace(stderr="", returncode=0)
        if cmd[-1] == str(webm_path):
            return SimpleNamespace(
                stderr="Input #0, matroska,webm\nStream #0:0: Video: vp8, yuv420p, 1280x720",
                returncode=0,
            )
        return SimpleNamespace(
            stderr="Duration: 00:00:10.00\nStream #0:0: Video: h264, yuv420p, 1280x720",
            returncode=0,
        )

    monkeypatch.setattr(media.imageio_ffmpeg, "get_ffmpeg_exe", lambda: "ffmpeg")
    monkeypatch.setattr(media.subprocess, "run", fake_run)
    monkeypatch.setattr(media, "ensure_min_quality", lambda path: path)

    final_path = _convert_to_mp4(str(webm_path))

    assert final_path == str(mp4_path)
    assert os.path.exists(final_path)
    assert not webm_path.exists()
    assert any(cmd[-1] == str(mp4_path) for cmd in calls)


# ── Browser session reuse for Cloudflare-protected media ──────────────────────

class FakeRequest:
    """Stands in for a browser context's request API, which carries its cookies."""

    def __init__(self, status=200, error=None):
        self.status = status
        self.error = error
        self.calls = []

    async def head(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.error:
            raise self.error
        return SimpleNamespace(status=self.status)


@pytest.mark.asyncio
async def test_forbidden_check_goes_through_the_browser_session():
    request = FakeRequest(status=403)

    assert await _is_forbidden("https://cdn.example.com/v.mp4", request, "https://example.com/p") is True
    assert request.calls[0][0] == "https://cdn.example.com/v.mp4"
    assert request.calls[0][1]["headers"] == {"Referer": "https://example.com/p"}


@pytest.mark.asyncio
async def test_forbidden_check_allows_reachable_media_and_network_errors():
    assert await _is_forbidden("https://cdn.example.com/v.mp4", FakeRequest(status=206), "") is False
    assert await _is_forbidden(
        "https://cdn.example.com/v.mp4", FakeRequest(error=RuntimeError("timeout")), ""
    ) is False


@pytest.mark.asyncio
async def test_forbidden_check_rejects_blob_urls_without_a_request():
    request = FakeRequest()

    assert await _is_forbidden("blob:https://example.com/abc", request, "") is True
    assert request.calls == []


class FakeFrame:
    def __init__(self, url, playing=None):
        self.url = url
        self.playing = playing

    async def evaluate(self, js):
        return self.playing


@pytest.mark.asyncio
async def test_main_playing_video_reports_the_frame_that_plays_it():
    player = FakeFrame("https://player.example/embed/9", playing="https://player.example/get_file/9.mp4")
    page = FakeFrame("https://watch.example/v/9")
    page.frames = [page, FakeFrame("https://ads.example/frame"), player]

    assert await _get_main_playing_video_url(page) == (
        "https://player.example/get_file/9.mp4", "https://player.example/embed/9",
    )


def test_ffmpeg_cookies_keep_each_cookie_scoped_to_its_domain():
    cookies = [
        {"name": "cf_clearance", "value": "abc", "domain": ".example.com", "path": "/"},
        {"name": "PHPSESSID", "value": "x1", "domain": "www.example.com", "path": "/app"},
    ]

    assert _ffmpeg_cookies(cookies) == (
        "cf_clearance=abc; path=/; domain=.example.com;\n"
        "PHPSESSID=x1; path=/app; domain=www.example.com;"
    )


@pytest.mark.asyncio
async def test_direct_download_hands_browser_cookies_to_ffmpeg(monkeypatch):
    commands = []

    async def fake_exec(*cmd, **kwargs):
        commands.append(list(cmd))
        raise RuntimeError("stop after capturing the command")

    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", fake_exec)

    result = await _download_video_direct(
        "https://cdn.example.com/v.mp4",
        "https://example.com/p",
        "UA",
        cookies="cf_clearance=abc; path=/; domain=.example.com;",
    )

    assert result is None
    ffmpeg_cmd = commands[0]
    assert ffmpeg_cmd[ffmpeg_cmd.index("-cookies") + 1] == "cf_clearance=abc; path=/; domain=.example.com;"
    assert ffmpeg_cmd.index("-cookies") < ffmpeg_cmd.index("-i")


def test_download_budget_grows_with_video_length_for_paced_cdns():
    # A playable stream arrives at least in real time, so a paced CDN needs
    # up to the video's duration on top of the base budget.
    assert _download_budget_s(120, 482.0) == 602.0
    assert _download_budget_s(120, None) == 120
    assert _download_budget_s(120, 0.0) == 120


# ── Remote length probe ───────────────────────────────────────────────────────

def test_probe_reads_remote_length_with_referer_cookies_and_user_agent(monkeypatch):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stdout="", stderr="  Duration: 00:01:51.22, start: 0.000000, bitrate: 2 kb/s")

    monkeypatch.setattr(media.subprocess, "run", fake_run)

    length = probe_remote_duration(
        "https://v.example/one.mp4", "https://albums.example/a/1", user_agent="UA", cookies="c=1; domain=.v.example;"
    )

    assert length == pytest.approx(111.22)
    cmd = calls[0]
    assert cmd[cmd.index("-headers") + 1] == "Referer: https://albums.example/a/1\r\n"
    assert cmd[cmd.index("-user_agent") + 1] == "UA"
    assert cmd[cmd.index("-cookies") + 1] == "c=1; domain=.v.example;"
    assert cmd[-1] == "https://v.example/one.mp4"


def test_probe_returns_none_when_the_length_is_unknown(monkeypatch):
    results = iter([
        SimpleNamespace(returncode=1, stdout="", stderr="Server returned 403 Forbidden (access denied)"),
        SimpleNamespace(returncode=1, stdout="", stderr="  Duration: N/A, start: 0.000000, bitrate: N/A"),
    ])
    monkeypatch.setattr(media.subprocess, "run", lambda cmd, **kwargs: next(results))

    assert probe_remote_duration("https://v.example/a.mp4", "https://p.example") is None
    assert probe_remote_duration("https://v.example/live.m3u8", "https://p.example") is None


def test_probe_gives_up_after_its_timeout(monkeypatch):
    def slow(cmd, **kwargs):
        raise media.subprocess.TimeoutExpired(cmd, kwargs["timeout"])

    monkeypatch.setattr(media.subprocess, "run", slow)

    assert probe_remote_duration("https://v.example/a.mp4", "https://p.example") is None


# ── Referer ───────────────────────────────────────────────────────────────────

def test_browser_referer_sends_only_the_origin_across_origins():
    # Chrome's default policy (strict-origin-when-cross-origin); media hosts check for it.
    page = "https://www.albums.example/a/AbC123?x=1#top"

    assert browser_referer(page, "https://v81.albums.example/1/one.mp4") == "https://www.albums.example/"
    assert browser_referer(page, "https://www.albums.example/media/one.mp4") == "https://www.albums.example/a/AbC123?x=1"
    assert browser_referer("https://albums.example:8443/a/1", "https://cdn.example/1.mp4") == "https://albums.example:8443/"
    assert browser_referer(page, "http://www.albums.example/one.mp4") == ""  # https page to http: no Referer


@pytest.mark.asyncio
async def test_embed_download_time_limit_grows_with_the_videos_length(monkeypatch):
    # A 32-minute file from a CDN paced near playback speed needs more than the base budget.
    from app.services.scraper.media import _download_embed_video

    probes, timeouts = [], []

    def fake_probe(url, referer, user_agent="", cookies=""):
        probes.append((url, referer, user_agent))
        return 1917.0

    class FakeProc:
        returncode = None

        def kill(self):
            pass

        async def communicate(self):
            return b"", b""

    async def fake_exec(*cmd, **kwargs):
        return FakeProc()

    async def fake_wait_for(awaitable, timeout):
        awaitable.close()
        timeouts.append(timeout)
        raise asyncio.TimeoutError

    monkeypatch.setattr(media, "probe_remote_duration", fake_probe)
    monkeypatch.setattr(media.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(media.asyncio, "wait_for", fake_wait_for)

    assert await _download_embed_video("https://cdn.example/v.mp4", "https://example.com/", "UA") is None

    assert probes == [("https://cdn.example/v.mp4", "https://example.com/", "UA")]
    assert timeouts and set(timeouts) == {300 + 1917.0}
