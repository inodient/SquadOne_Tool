"""Playwright browser factory with stealth applied to the browser context."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from playwright.sync_api import Browser, BrowserContext, Playwright, sync_playwright
from playwright_stealth import Stealth

from core.config import AppConfig

_stealth = Stealth()


@contextmanager
def browser_context(config: AppConfig) -> Iterator[tuple[Playwright, Browser, BrowserContext]]:
    """Short-lived browser for planner perception; stealth init scripts on context."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=config.browser_headless)
        context = browser.new_context()
        _stealth.apply_stealth_sync(context)
        context.set_default_timeout(config.browser_timeout_ms)
        try:
            yield pw, browser, context
        finally:
            context.close()
            browser.close()


def new_page(context: BrowserContext):
    return context.new_page()
