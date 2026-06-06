"""LLM이 selector JSON만 생성하고, 고정 Playwright 템플릿에 주입합니다."""

from __future__ import annotations

import ast
import json
import re
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from core.config import AppConfig
from core.history import append_generation
from core.state import AgentState
from tools.sandbox_template import build_scraper_script, normalize_selector_json


def _extract_json_text(raw: str) -> str:
    """코드블록·주변 설명 제거 후 JSON 문자열 추출."""
    t = raw.strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)```", t, re.IGNORECASE)
    if m:
        return m.group(1).strip()
    if "{" in t and "}" in t:
        s = t.index("{")
        e = t.rindex("}") + 1
        return t[s:e].strip()
    return t


def run_generator(state: AgentState, app: AppConfig, llm: BaseChatModel) -> dict[str, Any]:
    run_id = state.get("run_id") or ""
    objective = state.get("objective") or ""
    url = state.get("url") or ""
    perception = state.get("page_perception") or ""
    plan = state.get("plan_steps") or []

    sys = SystemMessage(
        content=(
            "Return ONLY one JSON object (no markdown, no prose). Keys:\n"
            '- "sports_tab_selector": CSS selector for the tab/link that opens the sports section ("" if not needed)\n'
            '- "article_selector": CSS selector for each article container in the list\n'
            '- "title_link_selector": CSS selector relative to each container for the title link (often "a")\n'
            '- "meta_section": short label e.g. "스포츠" or "sports" for meta.section\n'
            "Selectors must be valid for Playwright query_selector / locator. "
            "Do not invent article titles; only selectors."
        )
    )
    human = HumanMessage(
        content=(
            f"Objective: {objective}\nStart URL: {url}\nPlan: {plan}\n"
            f"Page perception (truncated):\n{perception[:40_000]}"
        )
    )

    max_invokes = 1 if app.mock_llm else max(1, app.generator_llm_retries)
    messages: list = [sys, human]
    full_script = ""
    last_err = ""

    for attempt in range(max_invokes):
        resp = llm.invoke(messages)
        raw = str(resp.content if hasattr(resp, "content") else resp)
        payload = _extract_json_text(raw)
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError as e:
            last_err = str(e)
            if attempt < max_invokes - 1 and not app.mock_llm:
                messages.append(
                    HumanMessage(
                        content=f"JSON parse error: {e}. Reply with ONLY one JSON object with the required keys."
                    )
                )
            continue

        selectors = normalize_selector_json(parsed if isinstance(parsed, dict) else {})
        full_script = build_scraper_script(url, selectors)
        try:
            ast.parse(full_script)
        except SyntaxError as e:
            last_err = str(e)
            if attempt < max_invokes - 1 and not app.mock_llm:
                messages.append(
                    HumanMessage(
                        content=f"Generated script has SyntaxError: {e}. Reply with ONLY valid JSON selectors."
                    )
                )
            continue
        break
    else:
        full_script = build_scraper_script(url, normalize_selector_json({}))

    path = app.generated_script_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(full_script, encoding="utf-8")

    append_generation(
        app,
        run_id,
        planner_summary=str(state.get("plan_steps", ""))[:500],
        code_path=str(path),
        extra={"generator_last_error": last_err} if last_err else None,
    )

    return {"generated_script_path": str(path)}
