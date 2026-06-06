"""LangChain tools for the ReAct scraping agent.

All tools are bound to a BrowserSession instance via make_tools().
Each tool uses the Playwright Page object from the session directly.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from langchain_core.tools import tool

if TYPE_CHECKING:
    from tools.browser_session import BrowserSession


def make_tools(session: BrowserSession) -> list:
    """Factory: returns 8 LangChain tools bound to the given BrowserSession."""

    config = session._config

    # ------------------------------------------------------------------
    # navigate
    # ------------------------------------------------------------------

    @tool
    def navigate(url: str) -> str:
        """Navigate the browser to a URL. Call this first before inspecting any page.

        Args:
            url: Full URL to load (must include http:// or https://).

        Returns:
            Confirmation string with final URL and page title.
        """
        try:
            response = session.page.goto(url, wait_until="domcontentloaded")
            status = response.status if response else "?"
            return f"Navigated to: {session.page.url} | Title: {session.page.title()} | HTTP: {status}"
        except Exception as exc:  # noqa: BLE001
            return f"navigate error: {exc}"

    # ------------------------------------------------------------------
    # get_page_content
    # ------------------------------------------------------------------

    @tool
    def get_page_content() -> str:
        """Return accessibility tree + HTML summary + link patterns of the current page.

        Use this after every navigate or click to understand the page structure
        before deciding which selectors to use.

        Returns:
            JSON string with:
              "a11y"          — accessibility tree snapshot
              "html_summary"  — filtered HTML
              "link_patterns" — top 8 href patterns (numbers replaced with *), sorted by frequency.
                                Use this to discover article/news URL patterns on the page.
                                Example: ["/article/* (×12)", "/news/* (×5)"]
        """
        from tools.parser import build_page_perception  # reuse existing parser

        try:
            perception = json.loads(
                build_page_perception(
                    session.page,
                    a11y_max_chars=4_000,
                    html_max_chars=6_000,
                )
            )
        except Exception as exc:  # noqa: BLE001
            perception = {"a11y": "", "html_summary": f"[error: {exc}]"}

        # 동적 페이지(React/Vue)의 지연 렌더링을 기다린 후 링크 패턴 수집
        try:
            session.page.wait_for_load_state("networkidle", timeout=3_000)
        except Exception:  # noqa: BLE001
            pass  # timeout이면 현재 상태로 진행

        # Analyze link href patterns — and auto-suggest a selector when content patterns found
        _CONTENT_KEYWORDS = ["/article/", "/news/", "/post/", "/view/", "/story/", "/entry/"]
        try:
            link_patterns = session.page.evaluate("""
                () => {
                    const counts = {};
                    document.querySelectorAll('a[href]').forEach(a => {
                        const h = a.getAttribute('href') || '';
                        if (!h || h.startsWith('javascript') || h === '#') return;
                        // Normalize: replace numeric IDs and hash strings with *
                        const pat = h.replace(/\\/[0-9]+/g, '/*')
                                      .replace(/[a-f0-9]{8,}/g, '*')
                                      .split('?')[0];
                        counts[pat] = (counts[pat] || 0) + 1;
                    });
                    return Object.entries(counts)
                        .sort((a, b) => b[1] - a[1])
                        .slice(0, 8)
                        .map(([pat, n]) => pat + ' (×' + n + ')');
                }
            """)
            perception["link_patterns"] = link_patterns

            # Code-generated selector suggestion — removes need for LLM to reason about patterns
            for kw in _CONTENT_KEYWORDS:
                if any(kw in p for p in link_patterns):
                    perception["suggested_selector"] = f"a[href*='{kw}']"
                    perception["suggested_fields"] = {"title": "", "url": "[href]"}
                    break
        except Exception:  # noqa: BLE001
            perception["link_patterns"] = []

        return json.dumps(perception, ensure_ascii=False, indent=2)

    # ------------------------------------------------------------------
    # click
    # ------------------------------------------------------------------

    @tool
    def click(selector: str, index: int = 0) -> str:
        """Click an element matching the CSS selector.

        Args:
            selector: CSS selector for the target element.
            index: 0-based index when multiple elements match (default 0).

        Returns:
            Confirmation with the URL after the click.
        """
        try:
            session.page.locator(selector).nth(index).click()
            session.page.wait_for_load_state("domcontentloaded")
            return f"Clicked '{selector}'[{index}] | Current URL: {session.page.url}"
        except Exception as exc:  # noqa: BLE001
            return f"click error on '{selector}'[{index}]: {exc}"

    # ------------------------------------------------------------------
    # type_text
    # ------------------------------------------------------------------

    @tool
    def type_text(selector: str, text: str, press_enter: bool = False) -> str:
        """Type text into an input field.

        Args:
            selector: CSS selector for the input element.
            text: Text to type.
            press_enter: If True, press Enter after typing.

        Returns:
            Confirmation string.
        """
        try:
            session.page.locator(selector).fill(text)
            if press_enter:
                session.page.keyboard.press("Enter")
                session.page.wait_for_load_state("domcontentloaded")
            return f"Typed into '{selector}' | press_enter={press_enter}"
        except Exception as exc:  # noqa: BLE001
            return f"type_text error on '{selector}': {exc}"

    # ------------------------------------------------------------------
    # scroll
    # ------------------------------------------------------------------

    @tool
    def scroll(direction: str = "down", amount: int = 3) -> str:
        """Scroll the page to reveal more content (e.g. lazy-loaded items).

        Args:
            direction: "down" or "up".
            amount: Number of viewport heights to scroll.

        Returns:
            Confirmation string.
        """
        sign = 1 if direction == "down" else -1
        try:
            session.page.evaluate(
                f"window.scrollBy(0, {sign} * window.innerHeight * {amount})"
            )
            session.page.wait_for_timeout(500)
            return f"Scrolled {direction} {amount} viewport height(s)"
        except Exception as exc:  # noqa: BLE001
            return f"scroll error: {exc}"

    # ------------------------------------------------------------------
    # extract_structured_data
    # ------------------------------------------------------------------

    @tool
    def extract_structured_data(
        item_selector: str,
        fields: dict[str, str],
        max_items: int = 50,
    ) -> str:
        """Extract repeated structured data from the current page.

        Args:
            item_selector: CSS selector that matches each repeating container
                           (e.g. "article.news-card", "li.product",
                            "a[href*='article']").
            fields: Mapping of output field name to sub-selector. Three forms:
                    1. "" (empty string) — use the container's own inner_text().
                    2. "[attr]" (starts with "[") — get that attribute directly
                       from the container itself, e.g. "[href]", "[data-id]".
                    3. "css sub-selector" — find a descendant and get its text.
                       Append [href] to get the href of that descendant instead,
                       e.g. {"url": "a[href]"}.
            max_items: Maximum number of items to return.

        Returns:
            JSON string {"items": [...], "count": N, "selector": "..."}.
        """
        try:
            containers = session.page.query_selector_all(item_selector)
            results: list[dict[str, Any]] = []
            for container in containers[:max_items]:
                item: dict[str, Any] = {}
                for field_name, sub_selector in fields.items():
                    if sub_selector == "":
                        # "" → container's own text
                        item[field_name] = container.inner_text().strip()
                    elif sub_selector.startswith("[") and sub_selector.endswith("]"):
                        # "[attr]" → container's own attribute
                        attr = sub_selector[1:-1]
                        item[field_name] = container.get_attribute(attr) or ""
                    elif sub_selector.endswith("[href]"):
                        # "css[href]" → descendant's href attribute
                        real_selector = sub_selector[:-6]
                        el = container.query_selector(real_selector)
                        item[field_name] = el.get_attribute("href") if el else ""
                    else:
                        # plain sub-selector → descendant's text
                        el = container.query_selector(sub_selector)
                        item[field_name] = el.inner_text().strip() if el else ""
                results.append(item)
            return json.dumps(
                {"items": results, "count": len(results), "selector": item_selector},
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"items": [], "count": 0, "error": str(exc)})

    # ------------------------------------------------------------------
    # screenshot
    # ------------------------------------------------------------------

    @tool
    def screenshot(label: str = "") -> str:
        """Capture the current page as a PNG screenshot for debugging.

        Args:
            label: Optional label appended to the filename.

        Returns:
            File path where the screenshot was saved.
        """
        try:
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            name = f"screenshot_{label}_{ts}.png" if label else f"screenshot_{ts}.png"
            path = Path(config.react_outputs_dir) / name
            path.parent.mkdir(parents=True, exist_ok=True)
            session.page.screenshot(path=str(path), full_page=False)
            return f"Screenshot saved: {path}"
        except Exception as exc:  # noqa: BLE001
            return f"screenshot error: {exc}"

    # ------------------------------------------------------------------
    # done
    # ------------------------------------------------------------------

    @tool
    def done(result: dict[str, Any], success: bool = True, summary: str = "") -> str:
        """Signal that the extraction is complete. Always call this last.

        Args:
            result: Final structured data (any JSON-serializable dict).
            success: True if the objective was achieved, False otherwise.
            summary: Brief human-readable explanation of what was done.

        Returns:
            JSON sentinel string that the graph's tool_node interprets as completion.
        """
        return json.dumps(
            {"__done__": True, "result": result, "success": success, "summary": summary},
            ensure_ascii=False,
        )

    # ------------------------------------------------------------------
    # web_search
    # ------------------------------------------------------------------

    @tool
    def web_search(query: str, max_results: int = 5) -> str:
        """검색어로 관련 웹 페이지 URL 목록을 찾는다.

        URL이 제공되지 않았을 때 가장 먼저 호출해야 한다.
        네이버 검색 페이지를 Playwright로 직접 스크래핑한다.

        Args:
            query: 검색어 (한국어/영어 모두 가능).
            max_results: 반환할 최대 결과 수 (기본 5).

        Returns:
            JSON {"results": [{"title": "...", "url": "..."}], "count": N}
        """
        try:
            import urllib.parse as _up

            search_url = (
                f"https://search.naver.com/search.naver"
                f"?query={_up.quote(query)}&where=nexearch"
            )
            # networkidle 대기: Naver 검색 결과는 JS 렌더링 후 완성됨
            session.page.goto(search_url, wait_until="networkidle")
            session.page.wait_for_timeout(1500)

            results = session.page.evaluate(
                """
                (maxResults) => {
                    // Naver 포털 내비게이션/UI 도메인 제외 — sports/news 서브도메인은 허용
                    const EXCLUDED = [
                        'search.naver.com', 'www.naver.com', 'nid.naver.com',
                        'help.naver.com', 'policy.naver.com', 'mail.naver.com',
                        'blog.naver.com', 'cafe.naver.com', 'pay.naver.com',
                        'notify.naver.com', 'mybox.naver.com',
                        'section.cafe.naver.com', 'point.pay.naver.com',
                        'naver.com/more', 'naver.com/survey'
                    ];

                    const seen = new Set();
                    const items = [];

                    document.querySelectorAll('a[href^="http"]').forEach(a => {
                        if (items.length >= maxResults) return;
                        const href = a.href || '';
                        const title = a.innerText.trim();
                        if (!href || !title || title.length < 5) return;
                        if (seen.has(href)) return;
                        if (EXCLUDED.some(ex => href.includes(ex))) return;
                        seen.add(href);
                        items.push({title: title.slice(0, 100), url: href});
                    });
                    return items;
                }
                """,
                max_results,
            )

            return json.dumps(
                {"results": results, "count": len(results)},
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001
            return json.dumps({"results": [], "count": 0, "error": str(exc)})

    return [navigate, get_page_content, click, type_text, scroll, extract_structured_data, screenshot, done, web_search]
