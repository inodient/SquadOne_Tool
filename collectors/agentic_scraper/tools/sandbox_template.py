"""고정 Playwright 스크래퍼 템플릿. LLM은 CSS selector JSON만 채웁니다."""

from __future__ import annotations

import json
from typing import Any

SELECTOR_KEYS = ("sports_tab_selector", "article_selector", "title_link_selector", "meta_section")


def normalize_selector_json(raw: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(raw, dict):
        raw = {}
    out: dict[str, str] = {}
    for k in SELECTOR_KEYS:
        v = raw.get(k, "")
        out[k] = str(v).strip() if v is not None else ""
    return out


def build_scraper_script(start_url: str, selectors: dict[str, Any]) -> str:
    """URL·selector를 `json.dumps`로 이스케이프해 단일 실행 가능한 .py 문자열을 만듭니다."""
    sel = normalize_selector_json(selectors if isinstance(selectors, dict) else {})
    su = json.dumps(start_url, ensure_ascii=False)
    st = json.dumps(sel["sports_tab_selector"], ensure_ascii=False)
    ar = json.dumps(sel["article_selector"], ensure_ascii=False)
    tl = json.dumps(sel["title_link_selector"], ensure_ascii=False)
    ms = json.dumps(sel.get("meta_section", ""), ensure_ascii=False)

    return f"""#!/usr/bin/env python3
\"\"\"Playwright 고정 템플릿. selector 값만 Generator에서 주입됩니다.\"\"\"
from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from urllib.parse import urljoin

from playwright.async_api import async_playwright


START_URL = {su}
SPORTS_TAB_SELECTOR = {st}
ARTICLE_SELECTOR = {ar}
TITLE_LINK_SELECTOR = {tl}
META_SECTION_HINT = {ms}


async def run() -> None:
    run_id = os.environ["RUN_ID"]
    output_dir = Path(os.environ["OUTPUT_DIR"])
    output_dir.mkdir(parents=True, exist_ok=True)
    out = output_dir / (run_id + "_result.json")

    items: list[dict[str, str]] = []
    final_source = START_URL

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.goto(START_URL, wait_until="domcontentloaded", timeout=60_000)
            if SPORTS_TAB_SELECTOR:
                try:
                    loc = page.locator(SPORTS_TAB_SELECTOR).first
                    if await loc.count() > 0:
                        await loc.click(timeout=15_000)
                        await page.wait_for_load_state("domcontentloaded", timeout=30_000)
                        final_source = page.url
                except Exception:
                    pass

            if not ARTICLE_SELECTOR or not TITLE_LINK_SELECTOR:
                rows: list = []
            else:
                rows = await page.query_selector_all(ARTICLE_SELECTOR)

            for node in rows[:50]:
                link = await node.query_selector(TITLE_LINK_SELECTOR)
                if not link:
                    continue
                try:
                    title = (await link.inner_text() or "").strip()
                    href = (await link.get_attribute("href")) or ""
                    raw = str(await node.evaluate("el => el.outerHTML"))[:800]
                except Exception:
                    continue
                if not title:
                    continue
                abs_url = urljoin(page.url, href).split("#")[0]
                if not abs_url.startswith("http"):
                    abs_url = page.url
                items.append({{
                    "title": title,
                    "source_url": abs_url,
                    "selector": TITLE_LINK_SELECTOR,
                    "raw_snippet": raw,
                }})
        finally:
            await browser.close()

    section = META_SECTION_HINT or ("스포츠" if SPORTS_TAB_SELECTOR else "")
    data = {{
        "items": items,
        "meta": {{
            "section": section,
            "source_url": final_source,
        }},
    }}
    out.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
"""
