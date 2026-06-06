"""LangGraph ReAct loop: agent_node ↔ tool_node, terminates on done tool or max_steps."""

from __future__ import annotations

import json
import time
from typing import Any, Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, SystemMessage, ToolMessage
from langgraph.graph import END, START, StateGraph

# ToolMessages larger than this are replaced with a short summary in old steps
_LARGE_TOOL_MSG_CHARS = 500

from react_agent.prompts import build_system_prompt
from react_agent.state import ReactAgentState


def build_react_graph(config: Any, llm: BaseChatModel, session: Any):
    """Build and compile the ReAct StateGraph.

    Topology:
        START → agent_node ──(has tool calls)──→ tool_node ──→ agent_node
                     ↓                                 ↓
              (no tool calls /                  (is_done=True /
               step_count ≥ max)                step_count ≥ max)
                     ↓                                 ↓
                    END ←──────────────────────────── END
    """
    tools = session.as_tools()
    tool_registry: dict[str, Any] = {t.name: t for t in tools}
    llm_with_tools = llm.bind_tools(tools)
    sys_prompt = build_system_prompt()
    max_steps: int = config.react_max_steps

    # ------------------------------------------------------------------
    # Agent node: call LLM, decide next tool
    # ------------------------------------------------------------------

    def _trim_messages(messages: list) -> list:
        """Replace large ToolMessage bodies from old steps with short summaries.

        Keeps the last 2 ToolMessages intact so the LLM can read recent observations.
        All earlier large ToolMessages are replaced with a one-line summary to
        reduce context size and avoid TPM exhaustion.
        """
        tool_indices = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
        # Keep the last 2 ToolMessages verbatim
        to_truncate = set(tool_indices[:-2]) if len(tool_indices) > 2 else set()

        trimmed = []
        for i, m in enumerate(messages):
            if i in to_truncate and isinstance(m, ToolMessage):
                content_str = str(m.content)
                if len(content_str) > _LARGE_TOOL_MSG_CHARS:
                    summary = content_str[:200] + f"...[truncated {len(content_str)} chars]"
                    trimmed.append(ToolMessage(content=summary, tool_call_id=m.tool_call_id))
                else:
                    trimmed.append(m)
            else:
                trimmed.append(m)
        return trimmed

    def agent_node(state: ReactAgentState) -> dict:
        step_count = (state.get("step_count") or 0) + 1
        messages = list(state.get("messages") or [])

        # Prepend system message on the very first step
        if step_count == 1:
            messages = [SystemMessage(content=sys_prompt)] + messages

        # Trim large old ToolMessages to reduce token usage
        messages = _trim_messages(messages)

        # Rate-limit guard: pause before LLM call (configurable via REACT_STEP_DELAY_SEC)
        try:
            delay = float(getattr(config, "react_step_delay_sec", 0.0) or 0.0)
        except (TypeError, ValueError):
            delay = 0.0
        if delay > 0 and step_count > 1:
            time.sleep(delay)

        response: AIMessage = llm_with_tools.invoke(messages)
        return {"messages": [response], "step_count": step_count}

    # ------------------------------------------------------------------
    # Tool node: execute tool calls, handle 'done' specially
    # ------------------------------------------------------------------

    def tool_node(state: ReactAgentState) -> dict:
        messages = list(state.get("messages") or [])
        last_ai: AIMessage = messages[-1]  # type: ignore[assignment]

        tool_messages: list[ToolMessage] = []
        updates: dict[str, Any] = {}

        for tc in last_ai.tool_calls:
            tool_fn = tool_registry.get(tc["name"])
            if tool_fn is None:
                content = f"Unknown tool: {tc['name']}"
            else:
                try:
                    content = tool_fn.invoke(tc["args"])
                except Exception as exc:  # noqa: BLE001
                    content = f"Tool error ({tc['name']}): {exc}"

            tool_messages.append(
                ToolMessage(content=str(content), tool_call_id=tc["id"])
            )

            # Handle done tool — parse sentinel and set final state
            if tc["name"] == "done":
                try:
                    parsed = json.loads(str(content))
                    if parsed.get("__done__"):
                        updates["is_done"] = True
                        updates["final_result"] = parsed.get("result", {})
                        updates["success"] = parsed.get("success", True)
                except (json.JSONDecodeError, AttributeError):
                    updates["is_done"] = True
                    updates["final_result"] = {}
                    updates["success"] = False

        updates["messages"] = tool_messages
        return updates

    # ------------------------------------------------------------------
    # Routing functions
    # ------------------------------------------------------------------

    def route_after_agent(
        state: ReactAgentState,
    ) -> Literal["tools", "__end__"]:
        messages = list(state.get("messages") or [])
        last = messages[-1] if messages else None
        if not isinstance(last, AIMessage) or not getattr(last, "tool_calls", None):
            return "__end__"
        if (state.get("step_count") or 0) >= max_steps:
            return "__end__"
        return "tools"

    def route_after_tools(
        state: ReactAgentState,
    ) -> Literal["agent", "__end__"]:
        if state.get("is_done"):
            return "__end__"
        if (state.get("step_count") or 0) >= max_steps:
            return "__end__"
        return "agent"

    # ------------------------------------------------------------------
    # Build graph
    # ------------------------------------------------------------------

    g = StateGraph(ReactAgentState)
    g.add_node("agent", agent_node)
    g.add_node("tools", tool_node)

    g.add_edge(START, "agent")
    g.add_conditional_edges(
        "agent",
        route_after_agent,
        {"tools": "tools", "__end__": END},
    )
    g.add_conditional_edges(
        "tools",
        route_after_tools,
        {"agent": "agent", "__end__": END},
    )

    return g.compile()
