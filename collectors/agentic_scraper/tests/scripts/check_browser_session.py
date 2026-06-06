"""Standalone check: BrowserSession opens a browser and loads example.com.

Run from the project root:
    python tests/scripts/check_browser_session.py
"""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure project root is on sys.path
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import get_config
from tools.browser_session import BrowserSession


def main() -> None:
    config = get_config()
    print("Starting BrowserSession...")
    with BrowserSession(config) as session:
        session.page.goto("https://example.com", wait_until="domcontentloaded")
        title = session.page.title()
        url = session.page.url
    print(f"BrowserSession OK: url={url!r}  title={title!r}")


if __name__ == "__main__":
    main()
