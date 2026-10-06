"""Tests for how the scraper pipeline dresses new browser contexts."""

import pytest

from app.services.scraper import pipeline
from app.services.scraper.browser_adapter import ManagedBrowser
from app.services.scraper.playback import STEALTH_JS


class FakeContext:
    def __init__(self, kwargs):
        self.kwargs = kwargs
        self.init_scripts = []

    async def add_init_script(self, script):
        self.init_scripts.append(script)


class FakeBrowser:
    async def new_context(self, **kwargs):
        return FakeContext(kwargs)


@pytest.mark.asyncio
async def test_spoofing_providers_get_locale_and_stealth_script():
    browser = ManagedBrowser(FakeBrowser(), provider_name="agent-browser")

    ctx = await pipeline._open_context(browser, user_agent="UA", viewport={"width": 1, "height": 1})

    assert ctx.kwargs == {
        "user_agent": "UA",
        "viewport": {"width": 1, "height": 1},
        "locale": "en-US",
    }
    assert ctx.init_scripts == [STEALTH_JS]


@pytest.mark.asyncio
async def test_native_fingerprint_providers_keep_the_browser_locale_and_skip_stealth():
    browser = ManagedBrowser(FakeBrowser(), provider_name="patchright", native_fingerprint=True)

    ctx = await pipeline._open_context(browser, user_agent="UA", viewport={"width": 1, "height": 1})

    assert ctx.kwargs == {"user_agent": "UA", "viewport": {"width": 1, "height": 1}}
    assert ctx.init_scripts == []
