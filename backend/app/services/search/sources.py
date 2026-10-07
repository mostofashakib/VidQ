"""
Search sources. Each takes a query and a 1-based page number and returns
SearchResults in the source's own order.

- YtDlpSource runs yt-dlp's built-in site search (ytsearch, bilisearch).
- BrowserEngineSource loads a search engine's video results in the browser
  and harvests the outbound links, so it does not depend on engine markup.
  Engines page either by URL (an offset parameter) or by loading more
  results as the page scrolls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Protocol
from urllib.parse import urlencode

from playwright.async_api import async_playwright

from app.services.scraper.browser_adapter import launch_browser
from app.services.scraper.pipeline import _open_context
from app.services.scraper.playback import HEADLESS_OPTIONS, USER_AGENTS
from app.services.search.models import SearchResult
from app.services.search.page_reader import harvest_results

logger = logging.getLogger("VideoSearch")

Runner = Callable[[list[str]], Awaitable[str]]
UrlBuilder = Callable[[str, str, int], str]  # (query, safe_search, page) -> URL

_YTDLP_TIMEOUT_S = 60
_DDG_SAFE_SEARCH = {"off": "-2", "moderate": "-1", "strict": "1"}


class SourceError(Exception):
    """A source failed; the search carries on with the others."""


class SearchSource(Protocol):
    name: str

    async def search(self, query: str, page: int) -> list[SearchResult]:
        """Results for query on the given 1-based page."""


class PageFetcher(Protocol):
    async def fetch(self, url: str, scrolls: int) -> str:
        """The page's HTML after loading and scrolling `scrolls` times."""


async def _run_ytdlp(args: list[str]) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_YTDLP_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise SourceError(f"yt-dlp search timed out after {_YTDLP_TIMEOUT_S}s") from None
    if proc.returncode != 0:
        raise SourceError(stderr.decode(errors="replace").strip()[-300:] or f"exit code {proc.returncode}")
    return stdout.decode(errors="replace")


def _ytdlp_result(entry: dict, source: str) -> SearchResult | None:
    url = entry.get("webpage_url") or entry.get("url") or ""
    title = (entry.get("title") or "").strip()
    if not url.startswith("http") or not title:
        return None
    thumbnails = entry.get("thumbnails") or []
    thumbnail = (thumbnails[-1].get("url") if thumbnails else None) or entry.get("thumbnail") or ""
    duration = entry.get("duration")
    return SearchResult(
        url=url,
        title=title,
        source=source,
        duration=float(duration) if isinstance(duration, (int, float)) else None,
        thumbnail=thumbnail,
        snippet=(entry.get("description") or entry.get("channel") or "").strip(),
    )


class YtDlpSource:
    """yt-dlp's built-in search for one site, e.g. prefix 'ytsearch'."""

    def __init__(self, name: str, prefix: str, per_page: int = 10, runner: Runner | None = None) -> None:
        self.name = name
        self._prefix = prefix
        self._per_page = per_page
        self._runner = runner or _run_ytdlp

    async def search(self, query: str, page: int) -> list[SearchResult]:
        # yt-dlp has no offset, so page n fetches n pages and keeps the last one.
        count = self._per_page * page
        output = await self._runner([
            "yt-dlp", "--flat-playlist", "-j", "--no-warnings", "--socket-timeout", "20",
            f"{self._prefix}{count}:{query}",
        ])
        results = []
        for line in output.splitlines():
            try:
                result = _ytdlp_result(json.loads(line), self.name)
            except (ValueError, AttributeError):
                continue
            if result:
                results.append(result)
        return results[self._per_page * (page - 1):count]


def duckduckgo_videos_url(query: str, safe_search: str, page: int) -> str:
    """DuckDuckGo loads later pages on scroll, so the URL ignores page."""
    return f"https://duckduckgo.com/?{urlencode({'q': query})}&iax=videos&ia=videos&kp={_DDG_SAFE_SEARCH[safe_search]}"


def brave_videos_url(query: str, safe_search: str, page: int) -> str:
    params = {"q": query, "safesearch": safe_search}
    if page > 1:
        params["offset"] = page - 1
    return f"https://search.brave.com/videos?{urlencode(params)}"


class BrowserEngineSource:
    """
    A search engine's video tab, loaded in the browser. Only results that show
    a length are kept: video results do, the engine's own promo links do not.
    """

    def __init__(
        self,
        name: str,
        url_builder: UrlBuilder,
        safe_search: str,
        fetcher: PageFetcher,
        scroll_paging: bool = False,
    ) -> None:
        self.name = name
        self._url_builder = url_builder
        self._safe_search = safe_search
        self._fetcher = fetcher
        self._scroll_paging = scroll_paging

    async def search(self, query: str, page: int) -> list[SearchResult]:
        url = self._url_builder(query, self._safe_search, page)
        scrolls = page - 1 if self._scroll_paging else 0
        html = await self._fetcher.fetch(url, scrolls=scrolls)
        return [r for r in harvest_results(html, url, self.name) if r.duration]


class BrowserPageFetcher:
    """
    One browser shared by every engine page in a search round. Use it as an
    async context manager; pages load in parallel up to `concurrency`.
    """

    def __init__(self, settings, concurrency: int = 3, settle_s: float = 3.0, scroll_pause_s: float = 1.5) -> None:
        self._settings = settings
        self._semaphore = asyncio.Semaphore(concurrency)
        self._settle_s = settle_s
        self._scroll_pause_s = scroll_pause_s

    async def __aenter__(self) -> "BrowserPageFetcher":
        self._playwright = await async_playwright().start()
        try:
            self._browser = await launch_browser(
                self._playwright, self._settings, random.choice(USER_AGENTS), HEADLESS_OPTIONS
            )
            self._user_agent = await self._browser.native_user_agent()
        except Exception:
            await self._playwright.stop()
            raise
        return self

    async def __aexit__(self, *exc) -> None:
        try:
            await self._browser.close()
        finally:
            await self._playwright.stop()

    async def fetch(self, url: str, scrolls: int) -> str:
        async with self._semaphore:
            context = await _open_context(
                self._browser, user_agent=self._user_agent, viewport={"width": 1920, "height": 1080}
            )
            try:
                page = await context.new_page()
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
                await asyncio.sleep(self._settle_s)
                for _ in range(scrolls):
                    await page.mouse.wheel(0, 6000)
                    await asyncio.sleep(self._scroll_pause_s)
                return await page.content()
            finally:
                await context.close()


def default_sources(settings) -> Callable[[], AsyncIterator[list[SearchSource]]]:
    """
    Opener for the standard sources: yt-dlp search for YouTube and Bilibili,
    plus DuckDuckGo and Brave video search in one shared browser. When the
    browser fails to start, the search runs on the yt-dlp sources alone.
    """

    @asynccontextmanager
    async def open_sources():
        ytdlp = [YtDlpSource("youtube", "ytsearch"), YtDlpSource("bilibili", "bilisearch")]
        async with AsyncExitStack() as stack:
            try:
                fetcher = await stack.enter_async_context(BrowserPageFetcher(settings))
            except Exception as exc:
                logger.warning(f"Browser unavailable for search engines; using yt-dlp only: {exc}")
                yield ytdlp
                return
            safe = settings.search_safe_search
            yield ytdlp + [
                BrowserEngineSource("duckduckgo", duckduckgo_videos_url, safe, fetcher, scroll_paging=True),
                BrowserEngineSource("brave", brave_videos_url, safe, fetcher),
            ]

    return open_sources
