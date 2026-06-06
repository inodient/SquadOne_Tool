"""Validate final_data; LLM qualitative check; bump retry_count on failure."""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import HumanMessage, SystemMessage

from core.config import AppConfig
from core.history import history_path, set_last_generation_critic_feedback
from core.state import AgentState


def _parse_json_block(text: str) -> dict[str, Any]:
    t = text.strip()
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", t)
    if fence:
        t = fence.group(1).strip()
    return json.loads(t)


def run_critic(state: AgentState, app: AppConfig, llm: BaseChatModel) -> dict[str, Any]:
    objective = state.get("objective") or ""
    final_data = state.get("final_data") or {}
    run_id = state.get("run_id") or ""

    items = final_data.get("items") if isinstance(final_data, dict) else None
    meta = final_data.get("meta") if isinstance(final_data, dict) else None
    schema_ok = (
        isinstance(final_data, dict)
        and not final_data.get("_parse_error")
        and not final_data.get("_schema_error")
        and isinstance(items, list)
        and isinstance(meta, dict)
    )
    evidence_ok = False
    if schema_ok and isinstance(items, list):
        evidence_ok = len(items) > 0 and all(
            isinstance(i, dict)
            and str(i.get("title", "")).strip()
            and str(i.get("source_url", "")).startswith("http")
            and str(i.get("selector", "")).strip()
            and str(i.get("raw_snippet", "")).strip()
            for i in items
        )
    objective_hint = str(objective).lower()
    section_text = str((meta or {}).get("section", "")).lower() if isinstance(meta, dict) else ""
    # If user asks for sports-like section, require section/title hints to include sports terms.
    if any(k in objective_hint for k in ("스포츠", "sport")):
        sports_in_items = any(
            any(k in str((i or {}).get("title", "")).lower() for k in ("스포츠", "sport"))
            for i in (items or [])
        )
        evidence_ok = evidence_ok and (
            any(k in section_text for k in ("스포츠", "sport")) or sports_in_items
        )

    rule_ok = schema_ok and evidence_ok
    sys = SystemMessage(
        content=(
            'Reply with JSON only: {"success": true/false, "feedback": "short reason"}. '
            "success if scraped data plausibly satisfies the objective."
        )
    )
    human = HumanMessage(
        content=(
            f"Objective: {objective}\n"
            "Validation constraints: data must come from real DOM extraction and include evidence fields "
            "(title/source_url/selector/raw_snippet) for each item.\n"
            f"Data: {json.dumps(final_data, ensure_ascii=False)[:20_000]}"
        )
    )
    resp = llm.invoke([sys, human])
    raw = str(resp.content if hasattr(resp, "content") else resp)
    try:
        verdict = _parse_json_block(raw)
    except (json.JSONDecodeError, ValueError):
        verdict = {"success": rule_ok, "feedback": raw[:800]}
    llm_ok = bool(verdict.get("success"))
    feedback = str(verdict.get("feedback", ""))

    success = rule_ok and llm_ok
    retry_count = int(state.get("retry_count") or 0)
    if not success:
        retry_count += 1

    if run_id:
        set_last_generation_critic_feedback(app, run_id, feedback)

    return {
        "success": success,
        "critic_feedback": feedback,
        "retry_count": retry_count,
        "history_record_id": str(history_path(app, run_id)),
    }
