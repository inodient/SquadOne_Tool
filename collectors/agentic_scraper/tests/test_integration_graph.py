"""Integration: full graph with mock LLM and no browser."""

from __future__ import annotations

import pytest

from core.graph import build_compiled_graph
from core.llm import QueueFakeChatModel, default_fake_responses
from core.state import default_agent_state

_SEL_EXAMPLE_OK = (
    '{"sports_tab_selector": "", "article_selector": "body", "title_link_selector": "h1", "meta_section": "general"}'
)
_SEL_NO_MATCH = (
    '{"sports_tab_selector": "", "article_selector": ".nonexistent_selector_xyz", '
    '"title_link_selector": "a", "meta_section": ""}'
)


@pytest.mark.integration
def test_graph_single_pass_mock(tmp_app) -> None:
    app = tmp_app
    llm = QueueFakeChatModel(queue=list(default_fake_responses()))
    g = build_compiled_graph(app, llm=llm)
    init = default_agent_state("https://example.com", "extract mock", "int-1")
    out = g.invoke(init)
    assert out.get("success") is True
    assert out.get("plan_steps")
    data_dir = app.data_dir
    exports = list(data_dir.glob("int-1_*.json"))
    assert exports, "finalize should write data JSON"


@pytest.mark.integration
def test_graph_retry_then_success(tmp_app) -> None:
    app = tmp_app
    planner = '{"plan_steps": ["a"], "summary": "s"}'
    critic_fail = '{"success": false, "feedback": "empty"}'
    critic_ok = '{"success": true, "feedback": "ok"}'
    llm = QueueFakeChatModel(
        queue=[planner, _SEL_NO_MATCH, critic_fail, _SEL_EXAMPLE_OK, critic_ok]
    )
    g = build_compiled_graph(app, llm=llm)
    init = default_agent_state("https://example.com", "need items", "int-2")
    out = g.invoke(init)
    assert out.get("success") is True
    assert int(out.get("retry_count") or 0) >= 1


@pytest.mark.integration
def test_graph_exhaust_retries(tmp_app) -> None:
    app = tmp_app
    app = app.model_copy(update={"max_retries": 2})
    planner = '{"plan_steps": ["a"], "summary": "s"}'
    critic_fail = '{"success": false, "feedback": "empty"}'
    llm = QueueFakeChatModel(queue=[planner, _SEL_NO_MATCH, critic_fail, _SEL_NO_MATCH, critic_fail])
    g = build_compiled_graph(app, llm=llm)
    init = default_agent_state("https://example.com", "x", "int-3")
    out = g.invoke(init)
    assert out.get("success") is False
    assert int(out.get("retry_count") or 0) >= 1


@pytest.mark.integration
def test_graph_rejects_missing_evidence_schema(tmp_app) -> None:
    app = tmp_app
    planner = '{"plan_steps": ["a"], "summary": "s"}'
    gen_invalid = "```json\nnot valid json at all\n```"
    critic_any = '{"success": true, "feedback": "looks good"}'
    llm = QueueFakeChatModel(
        queue=[
            planner,
            gen_invalid,
            critic_any,
            gen_invalid,
            critic_any,
            gen_invalid,
            critic_any,
        ]
    )
    g = build_compiled_graph(app, llm=llm)
    init = default_agent_state("https://example.com", "스포츠 제목 추출", "int-4")
    out = g.invoke(init)
    assert out.get("success") is False
