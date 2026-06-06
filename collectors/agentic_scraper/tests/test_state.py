"""Unit: default agent state shape."""

from __future__ import annotations

import pytest

from core.state import default_agent_state


@pytest.mark.unit
def test_default_agent_state_keys() -> None:
    s = default_agent_state("https://example.com", "get title", "rid-1")
    assert s["url"] == "https://example.com"
    assert s["objective"] == "get title"
    assert s["run_id"] == "rid-1"
    assert s["plan_steps"] == []
    assert s["screenshot_paths"] == []
    assert s["retry_count"] == 0
    assert s["success"] is False
