"""ReactAgentState: LangGraph state for the ReAct scraping loop."""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from langchain_core.messages import BaseMessage
from langgraph.graph.message import add_messages


class ReactAgentState(TypedDict, total=False):
    # Full conversation history; add_messages reducer appends each step's messages
    messages: Annotated[list[BaseMessage], add_messages]

    # Run metadata
    url: str
    objective: str
    run_id: str

    # Execution control
    step_count: int
    is_done: bool  # set True when the 'done' tool fires

    # Accumulated results across pages/steps
    extracted_data: Annotated[list[dict[str, Any]], operator.add]
    screenshot_paths: Annotated[list[str], operator.add]

    # Final output populated by the 'done' tool
    final_result: dict[str, Any]
    success: bool
