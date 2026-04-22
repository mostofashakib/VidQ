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
