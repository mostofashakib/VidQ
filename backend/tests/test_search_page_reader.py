"""Tests for stripping search pages to content and collecting result links."""

from app.services.search.page_reader import harvest_results, page_text, strip_page

ENGINE = "https://search.engine.example/videos?q=ocean"

RESULTS_PAGE = """
<html><head><title>ocean - Videos</title><style>.x{color:red}</style>
<script>track()</script><link rel="preload" href="/a.js"></head>
<body>
  <header><a href="https://search.engine.example/settings">Settings</a></header>
  <nav><a href="https://partner.example/promo">Partner link in nav</a></nav>
  <div class="results">
    <div class="result" data-id="1" style="color:blue" onclick="go()">
      <a href="https://search.engine.example/l/?uddg=https%3A%2F%2Fvideos.example.org%2Fv%2F42">
        <img src="https://img.engine.example/thumb42.jpg" alt="">
        <span>Deep Sea Creatures (Full Documentary)</span>
      </a>
      <p>Explore the midnight zone with rare footage. <span class="dur">52:10</span></p>
    </div>
    <div class="result">
      <a href="https://www.youtube.com/watch?v=2pTGdH0A4Qs"><img data-src="https://i.example/yt.jpg"></a>
      <a href="https://www.youtube.com/watch?v=2pTGdH0A4Qs">The Coral Triangle</a>
      <span>1:12:46 · Natural World Facts</span>
    </div>
    <div class="sponsored-ad"><a href="https://shop.example/buy">Buy diving gear now</a></div>
    <div hidden><a href="https://hidden.example/v">Hidden result</a></div>
    <div aria-hidden="true"><a href="https://hidden2.example/v">Also hidden</a></div>
    <a href="javascript:void(0)">Load more</a>
    <a href="/videos?q=ocean&page=2">Next page</a>
  </div>
  <svg><a href="https://svg.example/x">svg link</a></svg>
  <iframe src="https://frame.example"></iframe>
  <form><input name="q"><button>Search</button></form>
  <footer><a href="https://about.example/">About</a></footer>
</body></html>
"""


def test_strip_page_removes_noise_tags_hidden_ads_and_attributes():
    soup = strip_page(RESULTS_PAGE)
    html = str(soup)

    for gone in ("<script", "<style", "<svg", "<iframe", "<form", "<header", "<nav", "<footer", "<link",
                 "Hidden result", "Also hidden", "Buy diving gear", "onclick", "data-id", "style=", "<img"):
        assert gone not in html
    assert 'href="https://www.youtube.com/watch?v=2pTGdH0A4Qs"' in html
    assert "Deep Sea Creatures (Full Documentary)" in html


def test_strip_page_keeps_image_sources_only_when_asked():
    html = str(strip_page(RESULTS_PAGE, keep_images=True))

    assert 'src="https://img.engine.example/thumb42.jpg"' in html
    assert 'data-src="https://i.example/yt.jpg"' in html
    assert "alt=" not in html


def test_page_text_is_compact_visible_text():
    text = page_text(RESULTS_PAGE, max_chars=80)

    assert "track()" not in text and "color:red" not in text
    assert "  " not in text and "\n" not in text
    assert len(text) <= 80
    assert text.startswith("ocean - Videos Deep Sea Creatures")


def test_harvest_collects_external_results_with_metadata():
    results = harvest_results(RESULTS_PAGE, ENGINE, source="engine")

    assert [(r.url, r.title) for r in results] == [
        ("https://videos.example.org/v/42", "Deep Sea Creatures (Full Documentary)"),
        ("https://www.youtube.com/watch?v=2pTGdH0A4Qs", "The Coral Triangle"),
    ]
    first, second = results
    assert first.duration == 3130.0
    assert first.thumbnail == "https://img.engine.example/thumb42.jpg"
    assert "midnight zone" in first.snippet
    assert first.source == "engine"
    assert second.duration == 4366.0
    assert second.thumbnail == "https://i.example/yt.jpg"


def test_harvest_ignores_untitled_links_and_the_engines_own_pages():
    html = """<body>
      <a href="https://videos.example.org/v/1"><img src="https://t.example/1.jpg"></a>
      <a href="https://cdn.engine.example/asset">Engine asset</a>
      <a href="https://videos.example.org/v/2">A real video title</a>
    </body>"""

    results = harvest_results(html, ENGINE, source="engine")

    assert [r.url for r in results] == ["https://videos.example.org/v/2"]


def test_titles_prefer_title_attributes_and_headings_over_card_text():
    html = """<body><ol>
      <li><a href="https://v.example/1">
        <img src="//proxy.engine.example/iu/?u=https%3A%2F%2Fthumbs.example%2F1.jpg&f=1">
        <p>20:07</p><h2 title="Deep Sea Worm Attacks Everything!"><span>Deep Sea Worm Attacks Eve...</span></h2>
        <span>1mo</span><span>179K views</span></a></li>
      <li><div><a href="https://v.example/2"><div>43:01</div></a>
        <a href="https://v.example/2"><span>YouTube</span><span>› Some Channel</span>
        <div title="The Abyss | Deep Sea Documentary">The Abyss | Deep Sea Documentary</div></a></div></li>
      <li><a href="https://v.example/3"><h3>Ocean Giants</h3> 12:00 · Channel</a></li>
    </ol></body>"""

    results = harvest_results(html, ENGINE, source="engine")

    assert [(r.title, r.duration) for r in results] == [
        ("Deep Sea Worm Attacks Everything!", 1207.0),
        ("The Abyss | Deep Sea Documentary", 2581.0),
        ("Ocean Giants", 720.0),
    ]
    assert results[0].thumbnail == "https://thumbs.example/1.jpg"
