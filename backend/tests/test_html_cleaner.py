"""Unit tests for HTML cleaning utilities."""
from app.services.scraper.html import clean_html, _clean_for_interaction


# ── clean_html ────────────────────────────────────────────────────────────────

def test_strips_script_tags():
    html = "<html><body><script>alert(1)</script><p>Hello</p></body></html>"
    assert "<script>" not in clean_html(html)
    assert "Hello" in clean_html(html)


def test_strips_style_tags():
    html = "<html><body><style>body{color:red}</style><p>Text</p></body></html>"
    assert "<style>" not in clean_html(html)
    assert "Text" in clean_html(html)


def test_strips_tracking_data_attrs():
    html = '<div data-tracking="ga" data-src="keep.mp4"><p>Content</p></div>'
    result = clean_html(html)
    assert "data-tracking" not in result
    assert 'data-src="keep.mp4"' in result


def test_strips_hidden_inputs():
    html = '<form><input type="hidden" name="csrf" value="secret"><p>Form</p></form>'
    assert 'type="hidden"' not in clean_html(html)


def test_reduces_html_size():
    noise = "<script>" + "x" * 500 + "</script>"
    html = f"<html><body>{noise}<p>Content</p></body></html>"
    result = clean_html(html)
    assert len(result) < len(html)
    assert "Content" in result


# ── _clean_for_interaction ────────────────────────────────────────────────────

def test_interaction_preserves_buttons():
    html = '<body><button class="accept-cookies" id="accept">Accept</button></body>'
    result = _clean_for_interaction(html)
    assert "accept" in result.lower()
    assert "<button" in result


def test_interaction_preserves_class_and_id():
    html = '<body><div class="consent-banner" id="gdpr"><button>OK</button></div></body>'
    result = _clean_for_interaction(html)
    assert 'class="consent-banner"' in result
    assert 'id="gdpr"' in result


def test_interaction_strips_scripts():
    html = "<body><script>console.log(1)</script><button>Play</button></body>"
    result = _clean_for_interaction(html)
    assert "<script>" not in result
    assert "<button>" in result


def test_interaction_truncates_long_html():
    long_html = "<p>" + "x" * 20_000 + "</p>"
    result = _clean_for_interaction(long_html, max_len=500)
    assert len(result) <= 600
    assert "truncated" in result


def test_interaction_does_not_strip_consent_elements():
    html = (
        '<body>'
        '<div class="cookie-consent">'
        '<button class="accept-btn">Accept All</button>'
        '</div>'
        '</body>'
    )
    result = _clean_for_interaction(html)
    assert "cookie-consent" in result
    assert "Accept All" in result


# ── player_html ───────────────────────────────────────────────────────────────

from app.services.scraper.html import player_html  # noqa: E402

PLAYER_PAGE = """<html><head><title>Deep Sea Creatures - Tube</title>
<meta property="og:video:duration" content="3130"><script>var big = "x".repeat(9)</script>
<style>.a{}</style></head><body>
<header><a href="/">Home</a><a href="/categories">Categories</a></header>
<nav>""" + "".join(f'<a href="/c/{n}">Category {n}</a>' for n in range(200)) + """</nav>
<main>
  <div class="content"><div id="player-wrap" class="video-player" data-tracking="x" style="width:100%">
    <video id="main" class="vjs-tech" src="https://cdn.example/v/main.mp4"></video>
    <button class="vjs-big-play-button" aria-label="Play">Play</button>
    <div class="vjs-control-bar"><button class="vjs-fullscreen-control">Fullscreen</button>
      <span class="vjs-duration">52:10</span></div>
  </div></div>
  <h1>Deep Sea Creatures</h1>
  <section class="related">""" + "".join(
    f'<a href="/v/{n}"><img src="/t/{n}.jpg"><span>Related video {n} with a long title</span></a>' for n in range(300)
) + """</section>
  <iframe src="https://ads.doubleclick.net/banner"></iframe>
</main>
<footer>""" + "Footer links " * 300 + """</footer>
</body></html>"""


def test_player_html_keeps_the_player_and_its_controls_and_little_else():
    context = player_html(PLAYER_PAGE)

    for kept in ('id="main"', "https://cdn.example/v/main.mp4", "vjs-big-play-button", 'aria-label="Play"',
                 "vjs-fullscreen-control", "52:10", "Deep Sea Creatures - Tube", "3130"):
        assert kept in context
    for dropped in ("Category 1", "Related video", "Footer links", "doubleclick", "<script", "repeat(9)",
                    "data-tracking", "style="):
        assert dropped not in context
    assert len(context) < len(PLAYER_PAGE) // 20


def test_player_html_keeps_an_embedded_player_frame():
    html = ('<html><body><nav>' + "menu " * 500 + '</nav><div class="video-container">'
            '<iframe src="https://player.example/embed/30163/" allowfullscreen></iframe></div></body></html>')

    context = player_html(html)

    assert 'src="https://player.example/embed/30163/"' in context
    assert "menu" not in context


def test_player_html_stays_within_its_budget():
    html = "<html><body>" + "".join(
        f'<div class="player-{n}"><video src="https://cdn.example/{n}.mp4"></video><p>{"words " * 200}</p></div>'
        for n in range(20)
    ) + "</body></html>"

    assert len(player_html(html, max_len=1500)) <= 1500


def test_player_html_falls_back_to_the_interactive_page_when_no_player_is_found():
    html = '<html><body><div class="consent"><button id="accept">Accept</button></div><p>Loading</p></body></html>'

    assert 'id="accept"' in player_html(html)
