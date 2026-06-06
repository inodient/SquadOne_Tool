"""Standalone check: exercise each browser tool against a live URL.

Run from the project root:
    python tests/scripts/check_tools.py
    python tests/scripts/check_tools.py --url https://news.ycombinator.com
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from core.config import get_config
from tools.browser_session import BrowserSession


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--url", default="https://example.com")
    args = p.parse_args()

    config = get_config()

    print(f"Target URL: {args.url}\n")
    with BrowserSession(config) as session:
        tools = {t.name: t for t in session.as_tools()}

        # 1. navigate
        print("[1] navigate")
        r = tools["navigate"].invoke({"url": args.url})
        print(f"    {r}\n")

        # 2. get_page_content
        print("[2] get_page_content")
        r = tools["get_page_content"].invoke({})
        data = json.loads(r)
        a11y_preview = data.get("a11y", "")[:200].replace("\n", " ")
        html_preview = data.get("html_summary", "")[:200].replace("\n", " ")
        print(f"    a11y (first 200 chars): {a11y_preview}")
        print(f"    html (first 200 chars): {html_preview}\n")

        # 3. scroll
        print("[3] scroll down")
        r = tools["scroll"].invoke({"direction": "down", "amount": 1})
        print(f"    {r}\n")

        # 4. extract_structured_data (generic attempt: find any links)
        print("[4] extract_structured_data  (selector=a, field=text)")
        r = tools["extract_structured_data"].invoke({
            "item_selector": "a",
            "fields": {"text": "a", "href": "a[href]"},
            "max_items": 5,
        })
        extracted = json.loads(r)
        print(f"    count={extracted['count']}")
        for item in extracted["items"][:3]:
            print(f"      {item}")
        print()

        # 5. screenshot
        print("[5] screenshot")
        r = tools["screenshot"].invoke({"label": "check_tools"})
        print(f"    {r}\n")

        # 6. done
        print("[6] done")
        r = tools["done"].invoke({
            "result": {"check": "ok"},
            "success": True,
            "summary": "check_tools completed",
        })
        print(f"    {r}\n")

    print("All tools checked successfully.")


if __name__ == "__main__":
    main()
