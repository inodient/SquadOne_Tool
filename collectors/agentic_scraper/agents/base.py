"""BaseAgent: run_id, single LLM instance per invoke, compiled graph runner."""

from __future__ import annotations

import logging
import uuid
from typing import Any

from core.config import AppConfig, get_config
from core.graph import build_compiled_graph
from core.state import AgentState, default_agent_state

logger = logging.getLogger(__name__)


class BaseAgent:
    """Thin runner around LangGraph with logging hooks and retry ceiling enforced in graph."""

    def __init__(self, config: AppConfig | None = None):
        self.config = config or get_config()

    def _make_run_id(self) -> str:
        return str(uuid.uuid4())

    def run(self, inp: dict[str, Any]) -> AgentState:
        url = str(inp.get("url") or "")
        objective = str(inp.get("objective") or "")
        run_id = str(inp.get("run_id") or self._make_run_id())
        logger.info("run start run_id=%s url=%s", run_id, url[:80])

        graph = build_compiled_graph(self.config)
        init = default_agent_state(url, objective, run_id)
        out = graph.invoke(init)
        logger.info("run end run_id=%s success=%s", run_id, out.get("success"))
        return out  # type: ignore[return-value]

    async def arun(self, inp: dict[str, Any]) -> AgentState:
        """Async wrapper: graph.ainvoke when callers are async."""
        url = str(inp.get("url") or "")
        objective = str(inp.get("objective") or "")
        run_id = str(inp.get("run_id") or self._make_run_id())
        graph = build_compiled_graph(self.config)
        init = default_agent_state(url, objective, run_id)
        out = await graph.ainvoke(init)
        return out  # type: ignore[return-value]
