"""Unit tests for SelectorCache — no browser, no LLM, no network."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from cache.selector_cache import SelectorCache


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture()
def cfg(tmp_path):
    config = MagicMock()
    config.cache_dir = tmp_path / "cache"
    config.cache_stale_days = 7
    config.cache_max_failures = 3
    return config


@pytest.fixture()
def cache(cfg):
    return SelectorCache(cfg)


STRATEGY = {
    "item_selector": "a[href*='/article/']",
    "fields": {"title": "", "url": "[href]"},
    "wait_ms": 0,
    "next_page_selector": None,
}


# ---------------------------------------------------------------------------
# Miss scenarios
# ---------------------------------------------------------------------------

def test_miss_on_empty_dir(cache):
    assert cache.get("https://example.com", "get titles") is None


def test_miss_after_invalidate(cache):
    cache.put("https://example.com", "titles", STRATEGY)
    key = cache._cache_key("https://example.com", "titles")
    cache.invalidate(key)
    assert cache.get("https://example.com", "titles") is None


def test_miss_after_max_failures(cache):
    cache.put("https://example.com", "titles", STRATEGY)
    key = cache._cache_key("https://example.com", "titles")
    cache.record_failure(key)
    cache.record_failure(key)
    cache.record_failure(key)
    assert cache.get("https://example.com", "titles") is None


def test_miss_on_stale_entry(tmp_path):
    """Entry created 10 days ago should be stale (stale_days=7)."""
    cfg = MagicMock()
    cfg.cache_dir = tmp_path / "cache"
    cfg.cache_stale_days = 7
    cfg.cache_max_failures = 3
    c = SelectorCache(cfg)
    c.put("https://example.com", "titles", STRATEGY)

    # Backdate created_at by 10 days
    key = c._cache_key("https://example.com", "titles")
    path = c._path(key)
    import json
    entry = json.loads(path.read_text())
    old_date = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    entry["created_at"] = old_date
    path.write_text(json.dumps(entry))

    assert c.get("https://example.com", "titles") is None


# ---------------------------------------------------------------------------
# Hit scenarios
# ---------------------------------------------------------------------------

def test_hit_after_put(cache):
    cache.put("https://example.com/page1", "titles", STRATEGY)
    result = cache.get("https://example.com/page1", "titles")
    assert result is not None
    assert result["item_selector"] == STRATEGY["item_selector"]
    assert result["fields"] == STRATEGY["fields"]


def test_hit_returns_strategy_fields_intact(cache):
    strategy = {"item_selector": ".card h2 a", "fields": {"title": "h2", "url": "a[href]"}, "wait_ms": 500}
    cache.put("https://site.com", "cards", strategy)
    result = cache.get("https://site.com", "cards")
    assert result["item_selector"] == ".card h2 a"
    assert result["wait_ms"] == 500


# ---------------------------------------------------------------------------
# Key semantics
# ---------------------------------------------------------------------------

def test_same_domain_different_paths_share_key(cache):
    """Different paths on same domain with same objective → same cache key."""
    key1 = cache._cache_key("https://news.naver.com/page1", "sports news")
    key2 = cache._cache_key("https://news.naver.com/page2", "sports news")
    assert key1 == key2


def test_different_objectives_different_keys(cache):
    key1 = cache._cache_key("https://example.com", "sports news")
    key2 = cache._cache_key("https://example.com", "stock prices")
    assert key1 != key2


def test_different_domains_different_keys(cache):
    key1 = cache._cache_key("https://naver.com", "news")
    key2 = cache._cache_key("https://daum.net", "news")
    assert key1 != key2


# ---------------------------------------------------------------------------
# Record success / failure
# ---------------------------------------------------------------------------

def test_record_success_resets_failures(cache):
    cache.put("https://example.com", "titles", STRATEGY)
    key = cache._cache_key("https://example.com", "titles")
    cache.record_failure(key)
    cache.record_failure(key)
    cache.record_success(key)
    cache.record_failure(key)
    # Only 1 consecutive failure after reset — should still be a hit
    assert cache.get("https://example.com", "titles") is not None


def test_record_success_increments_count(tmp_path):
    import json
    cfg = MagicMock()
    cfg.cache_dir = tmp_path / "cache"
    cfg.cache_stale_days = 7
    cfg.cache_max_failures = 3
    c = SelectorCache(cfg)
    c.put("https://example.com", "titles", STRATEGY)
    key = c._cache_key("https://example.com", "titles")
    c.record_success(key)
    c.record_success(key)
    entry = json.loads(c._path(key).read_text())
    assert entry["success_count"] == 3  # initial put counts as 1


# ---------------------------------------------------------------------------
# List / Clear
# ---------------------------------------------------------------------------

def test_list_entries_returns_all(cache):
    cache.put("https://a.com", "titles", STRATEGY)
    cache.put("https://b.com", "prices", STRATEGY)
    cache.put("https://c.com", "articles", STRATEGY)
    entries = cache.list_entries()
    assert len(entries) == 3
    domains = {e["domain"] for e in entries}
    assert "a.com" in domains and "b.com" in domains and "c.com" in domains


def test_clear_all(cache):
    cache.put("https://a.com", "titles", STRATEGY)
    cache.put("https://b.com", "prices", STRATEGY)
    deleted = cache.clear()
    assert deleted == 2
    assert cache.list_entries() == []


def test_clear_by_domain(cache):
    cache.put("https://naver.com", "news", STRATEGY)
    cache.put("https://daum.net", "news", STRATEGY)
    deleted = cache.clear(domain="naver.com")
    assert deleted == 1
    remaining = cache.list_entries()
    assert len(remaining) == 1
    assert remaining[0]["domain"] == "daum.net"


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------

def test_concurrent_put_get_thread_safety(cache):
    """20 threads simultaneously put and get on disjoint keys — no exceptions."""
    urls = [f"https://site{i}.com" for i in range(20)]
    errors = []

    def worker(url):
        try:
            cache.put(url, "titles", STRATEGY)
            result = cache.get(url, "titles")
            assert result is not None
        except Exception as exc:  # noqa: BLE001
            errors.append(str(exc))

    with ThreadPoolExecutor(max_workers=20) as pool:
        list(pool.map(worker, urls))

    assert errors == [], f"Thread safety errors: {errors}"
