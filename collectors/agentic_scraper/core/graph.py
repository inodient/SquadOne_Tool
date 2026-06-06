"""LangGraph: planner → generator → executor → critic → (retry | finalize)."""

from __future__ import annotations

from typing import Literal

from langchain_core.language_models.chat_models import BaseChatModel
from langgraph.graph import END, START, StateGraph

from agents.critic import run_critic
from agents.executor import run_executor
from agents.finalize import run_finalize
from agents.generator import run_generator
from agents.planner import run_planner
from core.config import AppConfig
from core.llm import get_chat_model
from core.state import AgentState


def _route_after_critic(state: AgentState, app: AppConfig) -> Literal["generator", "finalize"]:
    if state.get("success"):
        return "finalize"
    if int(state.get("retry_count") or 0) < app.max_retries:
        return "generator"
    return "finalize"


def build_compiled_graph(app: AppConfig, llm: BaseChatModel | None = None):
    llm = llm or get_chat_model(app)

    def planner_node(s: AgentState) -> dict:
        return run_planner(s, app, llm)

    def generator_node(s: AgentState) -> dict:
        return run_generator(s, app, llm)

    def executor_node(s: AgentState) -> dict:
        return run_executor(s, app)

    def critic_node(s: AgentState) -> dict:
        return run_critic(s, app, llm)

    def finalize_node(s: AgentState) -> dict:
        return run_finalize(s, app)

    g = StateGraph(AgentState)
    g.add_node("planner", planner_node)
    g.add_node("generator", generator_node)
    g.add_node("executor", executor_node)
    g.add_node("critic", critic_node)
    g.add_node("finalize", finalize_node)

    g.add_edge(START, "planner")
    g.add_edge("planner", "generator")
    g.add_edge("generator", "executor")
    g.add_edge("executor", "critic")
    g.add_conditional_edges(
        "critic",
        lambda s: _route_after_critic(s, app),
        {"generator": "generator", "finalize": "finalize"},
    )
    g.add_edge("finalize", END)
    return g.compile()
