"""Accessibility snapshot and bounded HTML summary for LLM perception."""

from __future__ import annotations

import json

from bs4 import BeautifulSoup, Tag
from playwright.sync_api import Page


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n...[truncated]..."


def a11y_tree_text(page: Page, *, max_nodes: int = 400) -> str:
    """
    Playwright 최신 Python API에서는 `page.accessibility`가 없고,
    `Locator.aria_snapshot()`이 YAML 형태의 접근성 스냅샷 문자열을 반환합니다.
    """
    try:
        raw = page.locator("html").aria_snapshot()
    except Exception:  # noqa: BLE001
        try:
            raw = page.locator("body").aria_snapshot()
        except Exception:  # noqa: BLE001
            return ""
    if not (raw or "").strip():
        return ""
    lines = raw.splitlines()
    if len(lines) > max_nodes:
        lines = lines[:max_nodes] + ["...[truncated]..."]
    return "\n".join(lines)


_ALLOWED_TAGS = frozenset(
    {
        "html",
        "body",
        "main",
        "article",
        "section",
        "nav",
        "h1",
        "h2",
        "h3",
        "p",
        "a",
        "ul",
        "ol",
        "li",
        "table",
        "tr",
        "td",
        "th",
        "form",
        "input",
        "button",
        "label",
    }
)


def html_summary(
    html: str,
    *,
    max_depth: int = 6,
    max_chars: int = 12_000,
) -> str:
    """Whitelist tags and strip noise; cap total characters for token budget."""
    soup = BeautifulSoup(html, "lxml")
    root: Tag = soup.body if soup.body else soup

    def render(el: Tag | str, depth: int) -> str:
        if depth > max_depth:
            return ""
        if isinstance(el, str):
            t = el.strip()
            return t[:200] if t else ""
        if not isinstance(el, Tag):
            return ""
        name = (el.name or "").lower()
        if name not in _ALLOWED_TAGS:
            return "".join(render(c, depth) for c in el.children)
        attrs: list[str] = []
        if name == "a" and el.get("href"):
            attrs.append(f'href="{str(el.get("href"))[:120]}"')
        if name == "input":
            for a in ("name", "type", "placeholder"):
                if el.get(a):
                    attrs.append(f'{a}="{str(el.get(a))[:80]}"')
        open_ = f"<{name}" + (" " + " ".join(attrs) if attrs else "") + ">"
        inner = "".join(render(c, depth + 1) for c in el.children)
        return open_ + inner + f"</{name}>"

    text = render(root, 0)
    return _truncate(text, max_chars)


def build_page_perception(page: Page, *, a11y_max_chars: int = 8000, html_max_chars: int = 12_000) -> str:
    """Combined a11y + innerHTML summary for planner state."""
    a11y = _truncate(a11y_tree_text(page), a11y_max_chars)
    try:
        content = page.content()
        html = html_summary(content, max_chars=html_max_chars)
    except Exception as exc:  # noqa: BLE001
        html = f"[html unavailable: {exc}]"
    return json.dumps(
        {"a11y": a11y, "html_summary": html},
        ensure_ascii=False,
        indent=2,
    )
