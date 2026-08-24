"""Tests for the agent-browser/Playwright browser provider adapters."""

from types import SimpleNamespace

import pytest

from app.config import Settings
from app.services.scraper.browser_adapter import (
    AgentBrowserAdapter,
    BrowserLaunchOptions,
    _extract_cdp_url,
    launch_browser,
)


class FakeBrowser:
    def __init__(self):
        self.closed = False
        self.context_kwargs = None

    async def new_context(self, **kwargs):
        self.context_kwargs = kwargs
        return "context"

    def is_connected(self):
        return not self.closed

    async def close(self):
        self.closed = True


class FakeChromium:
    def __init__(self):
        self.browser = FakeBrowser()
        self.launch_kwargs = None
        self.cdp_url = None

    async def launch(self, **kwargs):
        self.launch_kwargs = kwargs
        return self.browser

    async def connect_over_cdp(self, cdp_url):
        self.cdp_url = cdp_url
        return self.browser


def test_browser_is_headless_by_default_and_only_explicitly_overridden(monkeypatch):
    monkeypatch.delenv("BROWSER_HEADLESS", raising=False)
    assert Settings().browser_headless is True

    monkeypatch.setenv("BROWSER_HEADLESS", "false")
    assert Settings().browser_headless is False

    monkeypatch.setenv("BROWSER_HEADLESS", "invalid-value")
    assert Settings().browser_headless is True

def test_extracts_cdp_url_from_json_and_plain_text():
    assert (
        _extract_cdp_url('{"success":true,"data":{"cdpUrl":"ws://127.0.0.1:9222/devtools"}}')
        == "ws://127.0.0.1:9222/devtools"
    )
    assert _extract_cdp_url("CDP URL: http://127.0.0.1:9222") == "http://127.0.0.1:9222"


@pytest.mark.asyncio
async def test_agent_browser_launches_and_connects_over_cdp(monkeypatch):
    commands = []

    async def run_command(command):
        commands.append(list(command))
        if command[-2:] == ["get", "cdp-url"]:
            return '{"data":{"cdpUrl":"ws://127.0.0.1:9222/devtools/browser/test"}}'
        return '{"success":true}'

    monkeypatch.setattr("app.services.scraper.browser_adapter.shutil.which", lambda _: "/bin/agent-browser")
    chromium = FakeChromium()
    playwright = SimpleNamespace(chromium=chromium)
    adapter = AgentBrowserAdapter(command_runner=run_command)
    options = BrowserLaunchOptions(
        headless=True,
        user_agent="VidQ Test",
        chromium_args=("--no-sandbox", "--window-size=1920,1080"),
    )

    browser = await adapter.launch(playwright, options)

    assert browser.provider_name == "agent-browser"
    assert chromium.cdp_url == "ws://127.0.0.1:9222/devtools/browser/test"
    assert "--headed" in commands[0]
    assert commands[0][commands[0].index("--headed") + 1] == "false"
    assert commands[0][commands[0].index("--args") + 1] == (
        "--no-sandbox\n--window-size=1920,1080"
    )

    assert await browser.new_context(locale="en-US") == "context"
    assert chromium.browser.context_kwargs == {"locale": "en-US"}
    await browser.close()
    await browser.close()
    assert sum(command[-1] == "close" for command in commands) == 1
    assert chromium.browser.closed is True


@pytest.mark.asyncio
async def test_launch_browser_falls_back_to_playwright_when_agent_browser_is_missing(
    monkeypatch,
):
    monkeypatch.setattr("app.services.scraper.browser_adapter.shutil.which", lambda _: None)
    chromium = FakeChromium()
    playwright = SimpleNamespace(chromium=chromium)
    settings = SimpleNamespace(
        browser_provider="agent-browser",
        browser_headless=False,
        agent_browser_command="agent-browser",
    )

    browser = await launch_browser(
        playwright,
        settings,
        user_agent="VidQ Test",
        chromium_args=["--no-sandbox"],
    )

    assert browser.provider_name == "playwright"
    assert chromium.launch_kwargs == {
        "headless": False,
        "args": ["--no-sandbox"],
    }


@pytest.mark.asyncio
async def test_playwright_provider_can_be_selected_explicitly():
    chromium = FakeChromium()
    playwright = SimpleNamespace(chromium=chromium)
    settings = SimpleNamespace(
        browser_provider="playwright",
        browser_headless=True,
        agent_browser_command="agent-browser",
    )

    browser = await launch_browser(playwright, settings, "VidQ Test", [])

    assert browser.provider_name == "playwright"
    assert chromium.launch_kwargs == {"headless": True, "args": []}
