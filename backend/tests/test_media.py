"""Unit tests for ad URL filtering and video URL utilities."""
import os
from types import SimpleNamespace

import pytest

from app.services.scraper import media
from app.services.scraper.media import (
    _convert_to_mp4,
    _download_budget_s,
    _download_video_direct,
    _ffmpeg_cookies,
    _is_ad_video_url,
    _is_forbidden,
    _validate_video_file,
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
