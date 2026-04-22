"""Unit tests for ad URL filtering and video URL utilities."""
from app.services.scraper.media import _is_ad_video_url


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
