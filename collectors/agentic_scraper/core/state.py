"""
GlobalState / AgentState — LangGraph state keys are the integration contract.

Merge strategy: most keys are last-write-wins per node. Lists that accumulate across
steps use Annotated[..., operator.add] where noted.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class GlobalState(TypedDict, total=False):
    """
    Documented contract for external systems and SquadOne integration.
    Keys mirror LangGraph AgentState; keep this TypedDict in sync with AgentState.
    """

    url: str
    objective: str
    run_id: str
    page_perception: str
    plan_steps: list[str]
    generated_script_path: str
    execution_stdout: str
    execution_stderr: str
    screenshot_paths: list[str]
    retry_count: int
    critic_feedback: str
    final_data: dict[str, Any]
    success: bool
    history_record_id: str


class AgentState(TypedDict, total=False):
    """LangGraph state: same fields as GlobalState; screenshot_paths appends across nodes."""

    url: str
    objective: str
    run_id: str
    page_perception: str
    plan_steps: list[str]
    generated_script_path: str
    execution_stdout: str
    execution_stderr: str
    screenshot_paths: Annotated[list[str], operator.add]
    retry_count: int
    critic_feedback: str
    final_data: dict[str, Any]
    success: bool
    history_record_id: str


def default_agent_state(
    url: str,
    objective: str,
    run_id: str,
) -> AgentState:
    return AgentState(
        url=url,
        objective=objective,
        run_id=run_id,
        plan_steps=[],
        screenshot_paths=[],
        retry_count=0,
        success=False,
        final_data={},
    )
