"""Live E2E tests — require a real browser and LLM. Run with: pytest -m live"""

from __future__ import annotations

import pytest

from core.config import get_config
from react_agent.runner import ReactAgent


@pytest.mark.live
def test_live_hacker_news():
    """Extract top 5 story titles from Hacker News."""
    cfg = get_config()
    agent = ReactAgent(cfg)
    result = agent.run({
        "url": "https://news.ycombinator.com",
        "objective": "Extract the titles and URLs of the top 5 stories on the front page.",
    })
    assert result["success"], f"Expected success, got: {result}"
    items = result["result"].get("items") or []
    assert len(items) >= 1, "Expected at least 1 story"
    assert all("title" in item for item in items), "Each item should have a title"


@pytest.mark.live
def test_live_example_dot_com():
    """Simple sanity check on a static page."""
    cfg = get_config()
    agent = ReactAgent(cfg)
    result = agent.run({
        "url": "https://example.com",
        "objective": "Get the main heading text of the page.",
    })
    assert result["success"], f"Expected success, got: {result}"
