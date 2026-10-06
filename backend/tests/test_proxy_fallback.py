"""Tests for the Cloudflare proxy-rotation fallback in the scraper pipeline."""

import pytest

from app.services.scraper import pipeline
from app.services.scraper.browser_adapter import ManagedBrowser


class FakePage:
    pass


class FakeContext:
    def __init__(self, proxy):
        self.proxy = proxy
        self.closed = False

    async def new_page(self):
        page = FakePage()
        page.context = self
        return page

    async def close(self):
        self.closed = True


class FakeBrowser:
    def __init__(self):
        self.contexts = []

    async def new_context(self, **kwargs):
        ctx = FakeContext(kwargs.get("proxy"))
        self.contexts.append(ctx)
        return ctx


@pytest.fixture
def no_stealth(monkeypatch):
    async def _noop(ctx):
        return None

    monkeypatch.setattr(pipeline, "_inject_stealth", _noop)


@pytest.mark.asyncio
async def test_proxy_timeout_moves_on_to_direct_connection(monkeypatch, no_stealth):
    """A dead proxy that times out must not abort the job."""

    async def fake_goto(page, url):
        if page.context.proxy is not None:
            raise Exception(f"Page.goto: net::ERR_TIMED_OUT at {url}")

    monkeypatch.setattr(pipeline, "_safe_goto", fake_goto)
    browser = FakeBrowser()

    ctx, page = await pipeline._context_navigate_with_proxy_fallback(
        ManagedBrowser(browser, provider_name="agent-browser"),
        "https://example.com/v",
        ["http://1.2.3.4:3128"],
        {},
        max_proxy_tries=3,
    )

    assert ctx.proxy is None
    assert page.context is ctx
    assert browser.contexts[0].proxy == {"server": "http://1.2.3.4:3128"}
    assert browser.contexts[0].closed


@pytest.mark.asyncio
async def test_direct_connection_error_is_raised(monkeypatch, no_stealth):
    """When the final direct attempt fails, the error reaches the caller."""

    async def fake_goto(page, url):
        raise Exception(f"Page.goto: net::ERR_NAME_NOT_RESOLVED at {url}")

    monkeypatch.setattr(pipeline, "_safe_goto", fake_goto)
    browser = FakeBrowser()

    with pytest.raises(Exception, match="ERR_NAME_NOT_RESOLVED"):
        await pipeline._context_navigate_with_proxy_fallback(
            ManagedBrowser(browser, provider_name="agent-browser"),
            "https://example.com/v",
            [],
            {},
            max_proxy_tries=3,
        )
    assert all(ctx.closed for ctx in browser.contexts)
