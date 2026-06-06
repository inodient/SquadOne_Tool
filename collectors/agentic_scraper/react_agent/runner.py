"""ReactAgent: orchestrates one ReAct scraping run end-to-end.

Tier 1 — Selector Cache: if a proven strategy is cached for (domain, objective),
  run Playwright directly without LLM. Zero API cost.
Tier 2 — ReAct Agent: full LangGraph loop with LLM reasoning. Saves winning
  strategy to cache on success so future runs become Tier 1.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from core.config import AppConfig, get_config
from core.history import ensure_history, update_history_meta
from core.llm import get_chat_model
from react_agent.graph import build_react_graph
from react_agent.state import ReactAgentState
from tools.browser_session import BrowserSession

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Module-level helpers (used by ReactAgent and BatchExecutor)
# ---------------------------------------------------------------------------

def extract_strategy_from_messages(messages: list) -> dict | None:
    """Scan message history for the best extract_structured_data call that produced items.

    Scans all (AIMessage tool_call → ToolMessage result) pairs. Returns the call
    with the highest count > 0, as a strategy dict:
        {"item_selector": str, "fields": dict, "wait_ms": int, "next_page_selector": None}

    Returns None if no extract_structured_data call produced any items.
    """
    best_strategy: dict | None = None
    best_count = 0

    for i, m in enumerate(messages):
        if not isinstance(m, ToolMessage):
            continue
        ai_msg = next(
            (messages[j] for j in range(i - 1, -1, -1) if isinstance(messages[j], AIMessage)),
            None,
        )
        if ai_msg is None:
            continue
        matching_tc = next(
            (tc for tc in (ai_msg.tool_calls or [])
             if tc.get("id") == m.tool_call_id
             and tc.get("name") == "extract_structured_data"),
            None,
        )
        if matching_tc is None:
            continue
        try:
            parsed = json.loads(str(m.content))
        except (json.JSONDecodeError, AttributeError):
            continue
        count = parsed.get("count", 0)
        if count > best_count:
            best_count = count
            best_strategy = {
                "item_selector": matching_tc["args"].get("item_selector", ""),
                "fields": matching_tc["args"].get("fields", {}),
                "wait_ms": 0,
                "next_page_selector": None,
            }

    if best_strategy and best_strategy["item_selector"]:
        return best_strategy
    return None


# ---------------------------------------------------------------------------
# ReactAgent
# ---------------------------------------------------------------------------

class ReactAgent:
    """Manages browser lifecycle, graph construction, and result persistence for one run."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()

    def run(self, inp: dict[str, Any]) -> dict[str, Any]:
        """Execute one scraping run.

        Checks Tier 1 (selector cache) first. Falls back to Tier 2 (ReAct LLM)
        on cache miss or zero-item result. Saves winning strategy to cache on success.

        Args:
            inp: {"url", "objective", optional "run_id", optional "no_cache" (bool)}

        Returns:
            {"run_id", "success", "step_count", "result", "screenshot_paths", "cache_hit"}
        """
        url = inp.get("url") or ""
        objective = inp["objective"]
        run_id: str = inp.get("run_id") or str(uuid.uuid4())
        no_cache: bool = bool(inp.get("no_cache", False))
        search_query: str = inp.get("search_query") or ""

        logger.info("[ReactAgent] run_id=%s url=%s search=%s", run_id, url, search_query or "-")
        ensure_history(self.config, run_id, url or search_query, objective)

        # ── Tier 1: Selector Cache ────────────────────────────────────────────
        from cache.selector_cache import SelectorCache  # lazy import avoids circular

        cache = SelectorCache(self.config)
        cache_key = cache._cache_key(url, objective)

        if not no_cache and url:
            strategy = cache.get(url, objective)
            if strategy is not None:
                logger.info("[ReactAgent] Cache HIT run_id=%s", run_id)
                try:
                    cache_result = self._run_with_strategy(url, strategy)
                    if cache_result.get("count", 0) > 0:
                        cache.record_success(cache_key)
                        exported_path = self._save_result(run_id, cache_result, True)
                        update_history_meta(
                            self.config, run_id,
                            completed_success=True, step_count=0,
                            exported_data_path=str(exported_path) if exported_path else "",
                        )
                        return {
                            "run_id": run_id, "success": True, "step_count": 0,
                            "result": cache_result, "screenshot_paths": [],
                            "cache_hit": True,
                        }
                    # Cache hit but 0 items → site structure may have changed
                    logger.warning(
                        "[ReactAgent] Cache HIT but 0 items, falling back to Tier 2 run_id=%s", run_id
                    )
                    cache.record_failure(cache_key)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[ReactAgent] _run_with_strategy error: %s, falling back", exc)
                    cache.record_failure(cache_key)

        # ── Tier 2: ReAct Agent ───────────────────────────────────────────────
        llm = get_chat_model(self.config)

        with BrowserSession(self.config) as session:
            graph = build_react_graph(self.config, llm, session)

            # Build the opening human message — differs for URL vs search-query mode
            if search_query and not url:
                human_content = (
                    f"검색어: {search_query}\n"
                    f"Objective: {objective}\n\n"
                    f"URL이 제공되지 않았습니다. "
                    f"먼저 web_search('{search_query}')를 호출해 관련 URL을 찾은 뒤, "
                    f"가장 적합한 페이지로 이동해 objective를 완료하세요."
                )
            else:
                human_content = f"URL: {url}\nObjective: {objective}"

            initial_state: ReactAgentState = {
                "messages": [HumanMessage(content=human_content)],
                "url": url,
                "objective": objective,
                "run_id": run_id,
                "step_count": 0,
                "is_done": False,
                "extracted_data": [],
                "screenshot_paths": [],
                "success": False,
                "final_result": {},
            }

            recursion_limit = self.config.react_max_steps * 2 + 10
            final_state: ReactAgentState = graph.invoke(
                initial_state, config={"recursion_limit": recursion_limit}
            )

        result = final_state.get("final_result") or {}
        success = bool(final_state.get("success"))
        step_count = int(final_state.get("step_count") or 0)
        screenshot_paths = list(final_state.get("screenshot_paths") or [])

        # Fallback: recover from message history if done was never called
        if not success and not result:
            recovered = self._recover_from_messages(final_state.get("messages") or [])
            if recovered:
                result = recovered
                success = True
                logger.info("[ReactAgent] run_id=%s recovered result from message history", run_id)

        # ── Post-run: persist winning strategy to cache ───────────────────────
        if success and result and not no_cache and url:
            strategy = extract_strategy_from_messages(final_state.get("messages") or [])
            if strategy:
                cache.put(url, objective, strategy)

        exported_path = self._save_result(run_id, result, success)
        update_history_meta(
            self.config,
            run_id,
            completed_success=success,
            step_count=step_count,
            exported_data_path=str(exported_path) if exported_path else "",
        )

        logger.info(
            "[ReactAgent] run_id=%s success=%s steps=%d", run_id, success, step_count
        )

        return {
            "run_id": run_id,
            "success": success,
            "step_count": step_count,
            "result": result,
            "screenshot_paths": screenshot_paths,
            "cache_hit": False,
        }

    # ------------------------------------------------------------------
    # Tier 1 execution — no LLM
    # ------------------------------------------------------------------

    def _run_with_strategy(self, url: str, strategy: dict) -> dict:
        """Run extraction using a cached strategy. No LLM calls.

        Mirrors the extract_structured_data tool logic from react_agent/tools.py.
        Returns {"items": [...], "count": N}.
        """
        item_selector: str = strategy["item_selector"]
        fields: dict[str, str] = strategy["fields"]
        wait_ms: int = int(strategy.get("wait_ms") or 0)
        max_items = 200

        with BrowserSession(self.config) as session:
            session.page.goto(url, wait_until="domcontentloaded")
            if wait_ms > 0:
                session.page.wait_for_timeout(wait_ms)

            containers = session.page.query_selector_all(item_selector)
            raw_items: list[dict] = []
            for container in containers[:max_items]:
                item: dict[str, Any] = {}
                for field_name, sub_selector in fields.items():
                    if sub_selector == "":
                        item[field_name] = container.inner_text().strip()
                    elif sub_selector.startswith("[") and sub_selector.endswith("]"):
                        attr = sub_selector[1:-1]
                        item[field_name] = container.get_attribute(attr) or ""
                    elif sub_selector.endswith("[href]"):
                        real_sel = sub_selector[:-6]
                        el = container.query_selector(real_sel)
                        item[field_name] = el.get_attribute("href") if el else ""
                    else:
                        el = container.query_selector(sub_selector)
                        item[field_name] = el.inner_text().strip() if el else ""
                raw_items.append(item)

        clean = self._apply_noise_filters(raw_items)
        return {"items": clean, "count": len(clean)}

    # ------------------------------------------------------------------
    # Shared helpers
    # ------------------------------------------------------------------

    def _apply_noise_filters(self, items: list[dict]) -> list[dict]:
        """Deduplicate by URL and remove known noise entries (기사엔드, 바로가기, etc.)."""
        seen_urls: set[str] = set()
        clean: list[dict] = []
        for item in items:
            url = (item.get("url") or "").strip()
            title = (item.get("title") or "").strip()
            if not url and not title:
                continue
            if "기사엔드" in title or "바로가기" in title:
                continue
            if url and url in seen_urls:
                continue
            if url:
                seen_urls.add(url)
            clean.append(item)
        return clean

    def _recover_from_messages(self, messages: list) -> dict:
        """Scan message history for the last extract_structured_data result with items."""
        last_items: list = []
        for i, m in enumerate(messages):
            if not isinstance(m, ToolMessage):
                continue
            ai_msg = next(
                (messages[j] for j in range(i - 1, -1, -1) if isinstance(messages[j], AIMessage)),
                None,
            )
            if ai_msg is None:
                continue
            tool_name = next(
                (tc["name"] for tc in (ai_msg.tool_calls or [])
                 if tc.get("id") == m.tool_call_id),
                None,
            )
            if tool_name != "extract_structured_data":
                continue
            try:
                parsed = json.loads(str(m.content))
                items = parsed.get("items") or []
                if items:
                    last_items = items
            except (json.JSONDecodeError, AttributeError):
                continue
        clean = self._apply_noise_filters(last_items)
        return {"items": clean, "count": len(clean)} if clean else {}

    def _save_result(self, run_id: str, result: dict, success: bool) -> Path | None:
        if not result:
            return None
        out_dir = Path(self.config.react_outputs_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        path = out_dir / f"{run_id}_{ts}.json"
        path.write_text(
            json.dumps(
                {"run_id": run_id, "success": success, "result": result},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return path
