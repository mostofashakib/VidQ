"""Tests for finding a page's own embeddable player through oEmbed discovery."""

import httpx
import pytest

from app.services.search.embed import discover_embed

PAGE = """<html><head>
<link rel="alternate" type="application/json+oembed" href="https://site.example/oembed?url=https%3A%2F%2Fsite.example%2Fv%2F1">
</head><body>video</body></html>"""


def transport(oembed_html='<iframe width="200" src="https://site.example/embed/1?feature=oembed"></iframe>',
              page=PAGE, oembed_status=200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v/1":
            return httpx.Response(200, text=page, headers={"content-type": "text/html; charset=utf-8"})
        if request.url.path == "/oembed":
            return httpx.Response(oembed_status, json={"type": "video", "html": oembed_html})
        if request.url.path == "/moved":
            return httpx.Response(302, headers={"location": "http://10.0.0.5/v/1"})
        return httpx.Response(404)

    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_discover_embed_returns_the_iframe_source_from_the_oembed_answer():
    async with transport() as client:
        assert await discover_embed(client, "https://site.example/v/1") == "https://site.example/embed/1?feature=oembed"


@pytest.mark.asyncio
@pytest.mark.parametrize("client_kwargs", [
    {"page": "<html><head></head></html>"},  # no oEmbed link
    {"oembed_html": "<p>no player</p>"},
    {"oembed_html": '<iframe src="javascript:alert(1)"></iframe>'},
    {"oembed_html": '<iframe src="http://site.example/embed/1"></iframe>'},  # not https
    {"oembed_status": 500},
])
async def test_discover_embed_returns_none_without_a_safe_https_player(client_kwargs):
    async with transport(**client_kwargs) as client:
        assert await discover_embed(client, "https://site.example/v/1") is None


@pytest.mark.asyncio
async def test_discover_embed_never_follows_redirects_to_internal_hosts():
    async with transport() as client:
        assert await discover_embed(client, "https://site.example/moved") is None
