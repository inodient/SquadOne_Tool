"""Unit tests for BatchExecutor — ReactAgent mocked, no browser/LLM."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from batch.executor import BatchExecutor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_cfg(tmp_path):
    cfg = MagicMock()
    cfg.cache_dir = tmp_path / "cache"
    cfg.cache_stale_days = 7
    cfg.cache_max_failures = 3
    cfg.batch_max_workers = 4
    return cfg


def _success_result(url="https://a.com", items=None, cache_hit=False):
    return {
        "url": url,
        "run_id": "test-id",
        "success": True,
        "result": {"items": items or [{"title": "T", "url": url}], "count": 1},
        "screenshot_paths": [],
        "cache_hit": cache_hit,
    }


STRATEGY = {
    "item_selector": "a.link",
    "fields": {"title": "", "url": "[href]"},
    "wait_ms": 0,
    "next_page_selector": None,
}


# ---------------------------------------------------------------------------
# run_urls — single URL
# ---------------------------------------------------------------------------

def test_run_urls_single_url(tmp_path):
    cfg = _make_cfg(tmp_path)
    executor = BatchExecutor(cfg)

    with patch("react_agent.runner.ReactAgent") as MockAgent:
        instance = MockAgent.return_value
        instance.run.return_value = _success_result("https://a.com")
        results = executor.run_urls(["https://a.com"], "titles")

    assert len(results) == 1
    assert results[0]["success"] is True


# ---------------------------------------------------------------------------
# run_urls — preserves order
# ---------------------------------------------------------------------------

def test_run_urls_preserves_order(tmp_path):
    cfg = _make_cfg(tmp_path)
    executor = BatchExecutor(cfg)
    urls = [f"https://site{i}.com" for i in range(5)]

    with patch("react_agent.runner.ReactAgent") as MockAgent:
        instance = MockAgent.return_value
        instance.run.side_effect = [_success_result(u) for u in urls]
        results = executor.run_urls(urls, "titles")

    assert [r["url"] for r in results] == urls


# ---------------------------------------------------------------------------
# run_urls — graceful failure on one URL
# ---------------------------------------------------------------------------

def test_run_urls_handles_single_failure(tmp_path):
    cfg = _make_cfg(tmp_path)
    executor = BatchExecutor(cfg)
    urls = ["https://a.com", "https://b.com"]

    def side_effect(inp):
        if inp["url"] == "https://b.com":
            raise RuntimeError("connection refused")
        return _success_result(inp["url"])

    with patch("react_agent.runner.ReactAgent") as MockAgent:
        instance = MockAgent.return_value
        instance.run.side_effect = side_effect
        results = executor.run_urls(urls, "titles")

    assert results[0]["success"] is True
    assert results[1]["success"] is False
    assert "error" in results[1]


# ---------------------------------------------------------------------------
# run_urls — same domain reuses cache
# ---------------------------------------------------------------------------

def test_run_urls_same_domain_cache_reuse(tmp_path):
    """First URL seeds the cache; second URL on same domain hits Tier 1."""
    cfg = _make_cfg(tmp_path)
    from cache.selector_cache import SelectorCache
    real_cache = SelectorCache(cfg)

    executor = BatchExecutor(cfg)
    executor._cache = real_cache  # use real cache for this test

    urls = ["https://naver.com/page1", "https://naver.com/page2"]

    agent_call_count = 0

    def fake_run(inp):
        nonlocal agent_call_count
        agent_call_count += 1
        # Simulate successful extraction + strategy saved to cache
        real_cache.put(inp["url"], "titles", STRATEGY)
        return _success_result(inp["url"])

    with patch("react_agent.runner.ReactAgent") as MockAgent:
        instance = MockAgent.return_value
        instance.run.side_effect = fake_run
        # Pre-seed cache so second URL hits Tier 1
        real_cache.put("https://naver.com/page2", "titles", STRATEGY)
        instance._run_with_strategy.return_value = {"items": [{"title": "T"}], "count": 1}

        results = executor.run_urls(urls, "titles")

    assert len(results) == 2


# ---------------------------------------------------------------------------
# _next_url — query param pattern
# ---------------------------------------------------------------------------

def test_next_url_query_param(tmp_path):
    cfg = _make_cfg(tmp_path)
    executor = BatchExecutor(cfg)

    result = executor._next_url("https://site.com/news?page=1", 2, None)
    assert result == "https://site.com/news?page=2"


def test_next_url_p_param(tmp_path):
    cfg = _make_cfg(tmp_path)
    executor = BatchExecutor(cfg)

    result = executor._next_url("https://site.com/board?p=3", 4, None)
    assert result == "https://site.com/board?p=4"


def test_next_url_path_pattern(tmp_path):
    cfg = _make_cfg(tmp_path)
    executor = BatchExecutor(cfg)

    result = executor._next_url("https://site.com/news/page/1", 2, None)
    assert result == "https://site.com/news/page/2"


def test_next_url_custom_fn(tmp_path):
    cfg = _make_cfg(tmp_path)
    executor = BatchExecutor(cfg)

    fn = lambda url, n: f"https://site.com/custom/{n}"
    result = executor._next_url("https://site.com/custom/1", 2, fn)
    assert result == "https://site.com/custom/2"


def test_next_url_unknown_pattern_returns_none(tmp_path):
    cfg = _make_cfg(tmp_path)
    executor = BatchExecutor(cfg)

    result = executor._next_url("https://site.com/static-page", 2, None)
    assert result is None


# ---------------------------------------------------------------------------
# run_pages — stops on empty items
# ---------------------------------------------------------------------------

def test_run_pages_stops_on_empty_items(tmp_path):
    cfg = _make_cfg(tmp_path)
    from cache.selector_cache import SelectorCache
    real_cache = SelectorCache(cfg)

    executor = BatchExecutor(cfg)
    executor._cache = real_cache

    page_responses = {
        "https://site.com/news?page=1": [{"title": "A", "url": "https://a.com"}],
        "https://site.com/news?page=2": [{"title": "B", "url": "https://b.com"}],
        "https://site.com/news?page=3": [],  # triggers stop
    }

    def fake_run(inp):
        url = inp["url"]
        items = page_responses.get(url, [])
        real_cache.put(url, "articles", STRATEGY)
        return {"success": True, "result": {"items": items, "count": len(items)}, "cache_hit": False}

    with patch("react_agent.runner.ReactAgent") as MockAgent:
        instance = MockAgent.return_value
        instance.run.side_effect = fake_run
        instance._run_with_strategy.side_effect = lambda url, s: {
            "items": page_responses.get(url, []),
            "count": len(page_responses.get(url, [])),
        }
        result = executor.run_pages(
            "https://site.com/news?page=1", "articles", n_pages=5
        )

    assert result["pages_scraped"] == 2
    assert result["count"] == 2


# ---------------------------------------------------------------------------
# run_pages — deduplicates items across pages
# ---------------------------------------------------------------------------

def test_run_pages_deduplicates_across_pages(tmp_path):
    cfg = _make_cfg(tmp_path)
    from cache.selector_cache import SelectorCache
    real_cache = SelectorCache(cfg)

    executor = BatchExecutor(cfg)
    executor._cache = real_cache

    same_item = {"title": "Same Article", "url": "https://article.com/1"}

    def fake_run(inp):
        real_cache.put(inp["url"], "articles", STRATEGY)
        return {"success": True, "result": {"items": [same_item], "count": 1}, "cache_hit": False}

    with patch("react_agent.runner.ReactAgent") as MockAgent:
        instance = MockAgent.return_value
        instance.run.side_effect = fake_run
        instance._run_with_strategy.return_value = {"items": [same_item], "count": 1}

        result = executor.run_pages(
            "https://site.com/news?page=1", "articles", n_pages=3
        )

    # Despite 3 pages each returning the same item, count should be 1
    assert result["count"] == 1
