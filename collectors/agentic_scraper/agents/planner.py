"""Load URL, build perception, LLM plan_steps."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from core.config import AppConfig
from core.history import ensure_history, update_history_meta
from core.state import AgentState
from tools.browser import browser_context, new_page
from tools.parser import build_page_perception


def _parse_json_block(text: str) -> dict[str, Any]:
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
    if fence:
        t = fence.group(1).strip()
    return json.loads(t)


def run_planner(state: AgentState, app: AppConfig, llm: BaseChatModel) -> dict[str, Any]:
    url = state.get("url") or ""
    objective = state.get("objective") or ""
    run_id = state.get("run_id") or ""

    perception = "{}"
    if app.skip_browser:
        perception = json.dumps(
            {"a11y": "[skipped]", "html_summary": f"url={url}"},
            ensure_ascii=False,
            indent=2,
        )
    else:
        with browser_context(app) as (_pw, _b, ctx):
            page = new_page(ctx)
            page.goto(url, wait_until="domcontentloaded")
            perception = build_page_perception(page)

    sys = SystemMessage(
        content=(
            "You output only valid JSON with keys: plan_steps (array of short strings), "
            "summary (one string). No markdown."
        )
    )
    human = HumanMessage(
        content=f"URL: {url}\nObjective: {objective}\nPage perception:\n{perception[:50_000]}"
    )
    resp = llm.invoke([sys, human])
    raw = resp.content if hasattr(resp, "content") else str(resp)
    try:
        data = _parse_json_block(str(raw))
    except (json.JSONDecodeError, ValueError):
        data = {
            "plan_steps": ["Open URL", "Collect visible data", "Write result JSON"],
            "summary": str(raw)[:500],
        }
    steps = data.get("plan_steps") or []
    if isinstance(steps, list):
        plan_steps = [str(s) for s in steps]
    else:
        plan_steps = [str(steps)]
    summary = str(data.get("summary", ""))

    ensure_history(app, run_id, url, objective)
    update_history_meta(app, run_id, last_planner_summary=summary)

    return {
        "page_perception": perception,
        "plan_steps": plan_steps,
    }
