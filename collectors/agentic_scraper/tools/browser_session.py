"""Persistent Playwright browser session for one agent run.

A single BrowserSession keeps one Chromium context alive across all tool calls
within a run, preserving cookies, JS state, and navigation history.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from playwright.sync_api import Browser, BrowserContext, Page, Playwright, sync_playwright
from playwright_stealth import Stealth

if TYPE_CHECKING:
    from core.config import AppConfig

_stealth = Stealth()


class BrowserSession:
    """One Playwright Chromium context scoped to a single agent run.

    Use as a context manager:

        with BrowserSession(config) as session:
            tools = session.as_tools()
            # ... run graph ...
    """

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._playwright: Playwright | None = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        self._playwright = sync_playwright().start()
        launch_kwargs: dict = {"headless": self._config.browser_headless}
        if self._config.browser_slow_mo_ms > 0:
            launch_kwargs["slow_mo"] = self._config.browser_slow_mo_ms
        self._browser = self._playwright.chromium.launch(**launch_kwargs)
        self._context = self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        _stealth.apply_stealth_sync(self._context)
        self._context.set_default_timeout(self._config.browser_timeout_ms)
        self._page = self._context.new_page()

    def stop(self) -> None:
        try:
            if self._context:
                self._context.close()
            if self._browser:
                self._browser.close()
            if self._playwright:
                self._playwright.stop()
        except Exception:  # noqa: BLE001
            pass
        finally:
            self._page = None
            self._context = None
            self._browser = None
            self._playwright = None

    def __enter__(self) -> BrowserSession:
        self.start()
        return self

    def __exit__(self, *_: object) -> None:
        self.stop()

    # ------------------------------------------------------------------
    # Page access
    # ------------------------------------------------------------------

    @property
    def page(self) -> Page:
        """Return the active page, creating one if needed."""
        if self._page is None or self._page.is_closed():
            if self._context is None:
                raise RuntimeError("BrowserSession has not been started. Call start() first.")
            self._page = self._context.new_page()
        return self._page

    # ------------------------------------------------------------------
    # Tool factory
    # ------------------------------------------------------------------

    def as_tools(self) -> list:
        """Return LangChain tools bound to this session instance."""
        from react_agent.tools import make_tools  # local import to avoid circular deps

        return make_tools(self)
