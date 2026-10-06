"""Browser launch adapters for the extraction pipeline.

Agent Browser owns the default Chrome process. Playwright connects to that
process over CDP so the existing extraction pipeline can keep using its mature
network interception and MediaRecorder code. The Patchright provider drives the
installed Chrome with automation leaks patched, for sites behind interactive
Cloudflare checks. The direct Playwright launcher is retained as a transparent
fallback.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import uuid
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Any, Protocol

logger = logging.getLogger("BrowserAdapter")

CommandRunner = Callable[[Sequence[str]], Awaitable[str]]
DriverStarter = Callable[[], Awaitable[Any]]

# A headed Chrome (BROWSER_HEADLESS=false) opens off-screen, out of view.
_OFF_SCREEN_WINDOW_ARG = "--window-position=-32000,-32000"


class BrowserAdapter(Protocol):
    """Common interface implemented by browser launch providers."""

    async def launch(self, playwright: Any, options: "BrowserLaunchOptions") -> "ManagedBrowser":
        """Launch a browser and return the managed browser facade."""


@dataclass(frozen=True)
class BrowserLaunchOptions:
    headless: bool
    user_agent: str
    chromium_args: tuple[str, ...]


class ManagedBrowser:
    """Small facade that gives both providers the API used by the pipeline."""

    def __init__(
        self,
        browser: Any,
        provider_name: str,
        close_callback: Callable[[], Awaitable[None]] | None = None,
        native_fingerprint: bool = False,
    ) -> None:
        self._browser = browser
        self.provider_name = provider_name
        self._close_callback = close_callback
        # True when the provider's own fingerprint must reach sites untouched:
        # locale overrides and stealth patches make Cloudflare reject it.
        self.native_fingerprint = native_fingerprint
        self._closed = False

    async def new_context(self, **kwargs: Any) -> Any:
        return await self._browser.new_context(**kwargs)

    async def native_user_agent(self) -> str:
        """Return the launched engine's own user agent, minus the headless marker.

        Bot checks such as Cloudflare compare the claimed user agent with the
        engine's real version and platform, so a hardcoded string gets blocked.
        """
        session = await self._browser.new_browser_cdp_session()
        try:
            version = await session.send("Browser.getVersion")
        finally:
            await session.detach()
        return version["userAgent"].replace("HeadlessChrome/", "Chrome/")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._close_callback:
            await self._close_callback()
            return
        await self._browser.close()


class PlaywrightBrowserAdapter:
    """The original bundled-Chromium launch path."""

    async def launch(self, playwright: Any, options: BrowserLaunchOptions) -> ManagedBrowser:
        browser = await playwright.chromium.launch(
            headless=options.headless,
            args=list(options.chromium_args),
        )
        return ManagedBrowser(browser, provider_name="playwright")


class PatchrightBrowserAdapter:
    """Drive the installed Chrome through Patchright's leak-patched driver."""

    def __init__(self, start_driver: DriverStarter | None = None) -> None:
        self._start_driver = start_driver or _start_patchright

    async def launch(self, playwright: Any, options: BrowserLaunchOptions) -> ManagedBrowser:
        args = list(options.chromium_args)
        if not options.headless:
            args.append(_OFF_SCREEN_WINDOW_ARG)

        driver = await self._start_driver()
        try:
            browser = await driver.chromium.launch(
                channel="chrome",
                headless=options.headless,
                args=args,
            )
        except Exception:
            await driver.stop()
            raise

        async def close_patchright() -> None:
            try:
                await browser.close()
            finally:
                await driver.stop()

        return ManagedBrowser(
            browser,
            provider_name="patchright",
            close_callback=close_patchright,
            native_fingerprint=True,
        )


class AgentBrowserAdapter:
    """Launch Chrome through agent-browser and attach Playwright over CDP."""

    def __init__(
        self,
        command: str = "agent-browser",
        command_runner: CommandRunner | None = None,
    ) -> None:
        self._command = command
        self._command_runner = command_runner or _run_command

    async def launch(self, playwright: Any, options: BrowserLaunchOptions) -> ManagedBrowser:
        if not shutil.which(self._command):
            raise RuntimeError(f"agent-browser command not found: {self._command}")

        session_name = f"vidq-{uuid.uuid4().hex}"
        base_command = [self._command, "--session", session_name, "--json"]
        launch_command = [
            *base_command,
            "--headed",
            str(not options.headless).lower(),
            "--user-agent",
            options.user_agent,
        ]
        if options.chromium_args:
            # Newline separation preserves commas inside flag values such as
            # --window-size=1920,1080.
            launch_command.extend(["--args", "\n".join(options.chromium_args)])
        launch_command.append("open")

        try:
            await self._command_runner(launch_command)
            cdp_output = await self._command_runner([*base_command, "get", "cdp-url"])
            cdp_url = _extract_cdp_url(cdp_output)
            browser = await playwright.chromium.connect_over_cdp(cdp_url)
        except Exception:
            await self._close_session(base_command)
            raise

        async def close_agent_browser() -> None:
            try:
                await self._close_session(base_command)
            finally:
                # Closing the agent-browser session normally disconnects
                # Playwright. Close any remaining connection without masking the
                # original cleanup result.
                try:
                    if browser.is_connected():
                        await browser.close()
                except Exception as exc:
                    logger.debug("Playwright CDP disconnect failed: %s", exc)

        return ManagedBrowser(
            browser,
            provider_name="agent-browser",
            close_callback=close_agent_browser,
        )

    async def _close_session(self, base_command: Sequence[str]) -> None:
        try:
            await self._command_runner([*base_command, "close"])
        except Exception as exc:
            logger.debug("agent-browser session cleanup failed: %s", exc)


async def launch_browser(
    playwright: Any,
    settings: Any,
    user_agent: str,
    chromium_args: Sequence[str],
) -> ManagedBrowser:
    """Launch the configured provider, falling back to direct Playwright."""
    options = BrowserLaunchOptions(
        headless=settings.browser_headless,
        user_agent=user_agent,
        chromium_args=tuple(chromium_args),
    )
    provider = settings.browser_provider
    playwright_adapter = PlaywrightBrowserAdapter()

    if provider == "playwright":
        logger.info("Browser provider: playwright (configured)")
        return await playwright_adapter.launch(playwright, options)

    if provider == "patchright":
        try:
            browser = await PatchrightBrowserAdapter().launch(playwright, options)
            logger.info("Browser provider: patchright")
            return browser
        except Exception as exc:
            logger.warning(
                "patchright unavailable (%s); falling back to bundled Playwright Chromium",
                exc,
            )
            return await playwright_adapter.launch(playwright, options)

    if provider != "agent-browser":
        logger.warning(
            "Unknown BROWSER_PROVIDER=%r; using agent-browser with Playwright fallback",
            provider,
        )

    try:
        browser = await AgentBrowserAdapter(settings.agent_browser_command).launch(
            playwright, options
        )
        logger.info("Browser provider: agent-browser")
        return browser
    except Exception as exc:
        logger.warning(
            "agent-browser unavailable (%s); falling back to bundled Playwright Chromium",
            exc,
        )
        return await playwright_adapter.launch(playwright, options)


async def _start_patchright() -> Any:
    from patchright.async_api import async_playwright

    return await async_playwright().start()


async def _run_command(command: Sequence[str]) -> str:
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=30)
    except TimeoutError:
        process.kill()
        await process.communicate()
        raise RuntimeError("agent-browser command timed out") from None

    stdout_text = stdout.decode(errors="replace").strip()
    stderr_text = stderr.decode(errors="replace").strip()
    if process.returncode != 0:
        detail = stderr_text or stdout_text or f"exit code {process.returncode}"
        raise RuntimeError(detail)
    return stdout_text


def _extract_cdp_url(output: str) -> str:
    """Accept agent-browser JSON output and its plain-text compatibility form."""
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        payload = None

    found = _find_url(payload)
    if found:
        return found

    match = re.search(r"(?:wss?|https?)://[^\s\"']+", output)
    if match:
        return match.group(0).rstrip(",}")
    raise RuntimeError("agent-browser did not return a CDP URL")


def _find_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value if re.match(r"^(?:wss?|https?)://", value) else None
    if isinstance(value, dict):
        for key in ("cdpUrl", "cdp_url", "url", "wsEndpoint", "webSocketDebuggerUrl"):
            found = _find_url(value.get(key))
            if found:
                return found
        for nested in value.values():
            found = _find_url(nested)
            if found:
                return found
    if isinstance(value, list):
        for nested in value:
            found = _find_url(nested)
            if found:
                return found
    return None
