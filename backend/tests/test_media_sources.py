"""Tests for remembering which document requested each media URL."""

from app.services.scraper.media_sources import MediaSources

PAGE = "https://watch.example/video/123/title/"
PLAYER = "https://player.example//embed/30163/?promo=1"


def test_media_from_an_embedded_player_gets_that_players_referer():
    sources = MediaSources(PAGE)
    sources.add("https://player.example/get_file/1/abc/30163_hq.mp4", PLAYER)
    sources.add("https://cdn.example/key=x/30163_hq.mp4", PLAYER)

    assert sources.referer("https://player.example/get_file/1/abc/30163_hq.mp4") == PLAYER
    assert sources.referer("https://cdn.example/key=x/30163_hq.mp4") == "https://player.example/"


def test_media_from_the_page_itself_gets_the_pages_referer():
    sources = MediaSources(PAGE)
    sources.add("https://watch.example/media/v.mp4")
    sources.add("https://cdn.example/v.m3u8", PAGE)

    assert sources.referer("https://watch.example/media/v.mp4") == PAGE
    assert sources.referer("https://cdn.example/v.m3u8") == "https://watch.example/"
    assert sources.referer("https://cdn.example/never-seen.mp4") == "https://watch.example/"


def test_the_first_document_to_request_a_url_wins_and_blank_frames_count_as_the_page():
    sources = MediaSources(PAGE)
    sources.add("https://cdn.example/a.mp4", PLAYER)
    sources.add("https://cdn.example/a.mp4", PAGE)
    sources.add("https://cdn.example/b.mp4", "about:blank")

    assert sources.urls() == ["https://cdn.example/a.mp4", "https://cdn.example/b.mp4"]
    assert sources.referer("https://cdn.example/a.mp4") == "https://player.example/"
    assert sources.referer("https://cdn.example/b.mp4") == "https://watch.example/"
