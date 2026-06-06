"""Integration tests for the ReAct graph using mock LLM tool calls."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage


# ---------------------------------------------------------------------------
# Helpers: build fake AIMessage with tool_calls
# ---------------------------------------------------------------------------

def _ai_with_tool(name: str, args: dict, call_id: str = "c1") -> AIMessage:
    msg = AIMessage(content="")
    msg.tool_calls = [{"name": name, "args": args, "id": call_id}]
    return msg


def _tool_response(content: str, call_id: str = "c1") -> ToolMessage:
    return ToolMessage(content=content, tool_call_id=call_id)


# ---------------------------------------------------------------------------
# Fixture: mock config + session
# ---------------------------------------------------------------------------

@pytest.fixture()
def mock_config(tmp_path):
    cfg = MagicMock()
    cfg.react_max_steps = 10
    cfg.react_outputs_dir = tmp_path
    return cfg


@pytest.fixture()
def mock_session(tmp_path):
    session = MagicMock()
    session._config = MagicMock()
    session._config.react_outputs_dir = tmp_path

    page = MagicMock()
    page.url = "https://example.com"
    page.title.return_value = "Example"
    page.goto.return_value = MagicMock(status=200)
    session.page = page

    # as_tools() returns real tools bound to mock session
    from react_agent.tools import make_tools
    session.as_tools.return_value = make_tools(session)
    return session


# ---------------------------------------------------------------------------
# Test: done tool terminates the graph
# ---------------------------------------------------------------------------

def test_done_tool_terminates_graph(mock_config, mock_session):
    from react_agent.graph import build_react_graph

    done_payload = json.dumps({
        "__done__": True,
        "result": {"items": [{"title": "HN Top"}]},
        "success": True,
        "summary": "done",
    })

    # LLM sequence: 1) call navigate, 2) call done
    call_count = 0
    def fake_invoke(messages):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _ai_with_tool("navigate", {"url": "https://example.com"}, "t1")
        # after navigate observation, call done
        return _ai_with_tool(
            "done",
            {"result": {"items": [{"title": "HN Top"}]}, "success": True, "summary": "done"},
            "t2",
        )

    fake_llm = MagicMock()
    fake_llm.bind_tools.return_value = MagicMock(invoke=fake_invoke)

    graph = build_react_graph(mock_config, fake_llm, mock_session)

    final = graph.invoke({
        "messages": [HumanMessage(content="URL: https://example.com\nObjective: get titles")],
        "url": "https://example.com",
        "objective": "get titles",
        "run_id": "test-run",
        "step_count": 0,
        "is_done": False,
        "extracted_data": [],
        "screenshot_paths": [],
        "success": False,
        "final_result": {},
    })

    assert final["is_done"] is True
    assert final["success"] is True
    assert final["final_result"]["items"][0]["title"] == "HN Top"


# ---------------------------------------------------------------------------
# Test: max_steps guard terminates graph without done
# ---------------------------------------------------------------------------

def test_max_steps_guard(mock_config, mock_session):
    from react_agent.graph import build_react_graph

    mock_config.react_max_steps = 2

    call_count = 0
    def fake_invoke(messages):
        nonlocal call_count
        call_count += 1
        # always call navigate, never done
        return _ai_with_tool("navigate", {"url": "https://example.com"}, f"t{call_count}")

    fake_llm = MagicMock()
    fake_llm.bind_tools.return_value = MagicMock(invoke=fake_invoke)

    graph = build_react_graph(mock_config, fake_llm, mock_session)

    final = graph.invoke({
        "messages": [HumanMessage(content="URL: https://example.com\nObjective: test")],
        "url": "https://example.com",
        "objective": "test",
        "run_id": "test-steps",
        "step_count": 0,
        "is_done": False,
        "extracted_data": [],
        "screenshot_paths": [],
        "success": False,
        "final_result": {},
    })

    # Graph stopped without done — success should remain False
    assert not final.get("is_done")
    assert final.get("step_count", 0) >= mock_config.react_max_steps


# ---------------------------------------------------------------------------
# Test: agent ends immediately if LLM returns no tool calls
# ---------------------------------------------------------------------------

def test_no_tool_calls_ends_graph(mock_config, mock_session):
    from react_agent.graph import build_react_graph

    def fake_invoke(messages):
        return AIMessage(content="I don't know what to do.")

    fake_llm = MagicMock()
    fake_llm.bind_tools.return_value = MagicMock(invoke=fake_invoke)

    graph = build_react_graph(mock_config, fake_llm, mock_session)

    final = graph.invoke({
        "messages": [HumanMessage(content="URL: https://example.com\nObjective: test")],
        "url": "https://example.com",
        "objective": "test",
        "run_id": "test-notool",
        "step_count": 0,
        "is_done": False,
        "extracted_data": [],
        "screenshot_paths": [],
        "success": False,
        "final_result": {},
    })

    assert not final.get("is_done")
    assert final.get("step_count") == 1
