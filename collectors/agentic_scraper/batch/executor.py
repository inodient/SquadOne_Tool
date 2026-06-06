"""BatchExecutor: run extraction across N URLs or N pages with strategy reuse.

Each URL run is independent (owns its own BrowserSession).
The selector cache is shared across runs within the same executor instance.
"""

from __future__ import annotations

import logging
import re
import urllib.parse
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Callable

from cache.selector_cache import SelectorCache
from core.config import AppConfig, get_config

logger = logging.getLogger(__name__)


class BatchExecutor:
    """Execute scraping across multiple URLs or multiple pages with LLM cost minimization.

    For run_urls():
    - Groups URLs by domain.
    - Per domain, if not cached: seeds the cache by running Tier 2 on the first URL.
    - Remaining URLs run in parallel; each hits Tier 1 (cache).

    For run_pages():
    - Page 1: agent.run() (Tier 1 or 2 depending on cache state).
    - Pages 2-N: _run_with_strategy() directly (no LLM).
    - Next-page URL resolved via custom fn, ?page=N pattern, or /page/N path pattern.
    """

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or get_config()
        self._cache = SelectorCache(self.config)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def run_urls(
        self,
        urls: list[str],
        objective: str,
        *,
        max_workers: int | None = None,
    ) -> list[dict[str, Any]]:
        """Scrape a list of URLs, reusing strategies within each domain.

        Args:
            urls: List of fully qualified URLs.
            objective: Shared extraction objective for all URLs.
            max_workers: Concurrency limit. Defaults to config.batch_max_workers.

        Returns:
            List of result dicts aligned by index to the input urls list.
            Each dict: {url, run_id, success, result, cache_hit, error (on failure)}.
        """
        max_workers = max_workers or self.config.batch_max_workers
        results: dict[int, dict[str, Any]] = {}

        # Group by domain to identify which domains need cache seeding
        domain_to_indices: dict[str, list[int]] = {}
        for i, url in enumerate(urls):
            domain = urllib.parse.urlparse(url).netloc
            domain_to_indices.setdefault(domain, []).append(i)

        # Phase 1: seed uncached domains serially (ensures cache is ready before parallel phase)
        seed_indices: set[int] = set()
        for domain, indices in domain_to_indices.items():
            first_url = urls[indices[0]]
            if self._cache.get(first_url, objective) is None:
                idx = indices[0]
                seed_indices.add(idx)
                logger.info("[Batch] Seeding domain=%s url=%s", domain, first_url)
                results[idx] = self._safe_run(idx, first_url, objective)

        # Phase 2: all remaining URLs in parallel (Tier 1 cache hits)
        remaining = [i for i in range(len(urls)) if i not in seed_indices]
        if remaining:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                future_to_idx = {
                    pool.submit(self._safe_run, i, urls[i], objective): i
                    for i in remaining
                }
                for future in as_completed(future_to_idx):
                    idx = future_to_idx[future]
                    try:
                        results[idx] = future.result()
                    except Exception as exc:  # noqa: BLE001
                        results[idx] = {
                            "url": urls[idx], "success": False,
                            "error": str(exc), "result": {}, "cache_hit": False,
                        }

        return [results[i] for i in range(len(urls))]

    def run_pages(
        self,
        base_url: str,
        objective: str,
        n_pages: int,
        *,
        next_page_fn: Callable[[str, int], str | None] | None = None,
    ) -> dict[str, Any]:
        """Scrape N sequential pages, accumulating items across all pages.

        Args:
            base_url: URL of the first page.
            objective: Extraction objective applied to all pages.
            n_pages: Total pages to scrape.
            next_page_fn: Optional callable(current_url, next_page_number) → next URL or None.

        Returns:
            {"items": [...all deduplicated items...], "count": N,
             "pages_scraped": M, "success": bool}
        """
        from react_agent.runner import ReactAgent

        agent = ReactAgent(self.config)
        all_items: list[dict] = []
        current_url = base_url
        pages_scraped = 0

        for page_num in range(1, n_pages + 1):
            logger.info("[Batch] Page %d/%d url=%s", page_num, n_pages, current_url)

            if page_num == 1:
                outcome = agent.run({
                    "url": current_url,
                    "objective": objective,
                    "run_id": str(uuid.uuid4()),
                })
                page_items = outcome.get("result", {}).get("items") or []
            else:
                # Use cached strategy for page 2+
                strategy = (
                    self._cache.get(current_url, objective)
                    or self._cache.get(base_url, objective)  # same domain fallback
                )
                if strategy is None:
                    logger.warning("[Batch] No strategy for page %d — stopping", page_num)
                    break
                try:
                    raw = agent._run_with_strategy(current_url, strategy)
                    page_items = raw.get("items") or []
                except Exception as exc:  # noqa: BLE001
                    logger.error("[Batch] Page %d error: %s — stopping", page_num, exc)
                    break

            if not page_items:
                logger.info("[Batch] Page %d returned 0 items — stopping pagination", page_num)
                break

            all_items.extend(page_items)
            pages_scraped = page_num

            # Resolve next page URL
            if page_num < n_pages:
                next_url = self._next_url(current_url, page_num + 1, next_page_fn)
                if next_url is None:
                    logger.info("[Batch] Cannot determine next page URL — stopping")
                    break
                current_url = next_url

        # Deduplicate across pages
        seen_urls: set[str] = set()
        deduped: list[dict] = []
        for item in all_items:
            item_url = (item.get("url") or "").strip()
            if item_url and item_url in seen_urls:
                continue
            if item_url:
                seen_urls.add(item_url)
            deduped.append(item)

        return {
            "items": deduped,
            "count": len(deduped),
            "pages_scraped": pages_scraped,
            "success": len(deduped) > 0,
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _safe_run(self, idx: int, url: str, objective: str) -> dict[str, Any]:
        """Run agent.run() and catch any exception, tagging result with url."""
        from react_agent.runner import ReactAgent  # lazy import — avoid circular

        run_id = str(uuid.uuid4())
        try:
            result = ReactAgent(self.config).run({
                "url": url, "objective": objective, "run_id": run_id,
            })
            result["url"] = url
            return result
        except Exception as exc:  # noqa: BLE001
            logger.error("[Batch] idx=%d url=%s error: %s", idx, url, exc)
            return {
                "url": url, "run_id": run_id, "success": False,
                "error": str(exc), "result": {}, "cache_hit": False,
            }

    def _next_url(
        self,
        current_url: str,
        next_page_num: int,
        next_page_fn: Callable[[str, int], str | None] | None,
    ) -> str | None:
        """Resolve the URL for the next page.

        Priority:
        1. next_page_fn(current_url, next_page_num) if provided.
        2. Query param pattern: ?page=N, ?p=N, ?pg=N, ?pageNo=N, ?pageNum=N.
        3. Path pattern: /page/N.
        4. Returns None if cannot determine.
        """
        if next_page_fn is not None:
            return next_page_fn(current_url, next_page_num)

        parsed = urllib.parse.urlparse(current_url)
        qs = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)

        for param in ("page", "p", "pg", "pageNo", "pageNum"):
            if param in qs:
                qs[param] = [str(next_page_num)]
                new_qs = urllib.parse.urlencode(qs, doseq=True)
                return urllib.parse.urlunparse(parsed._replace(query=new_qs))

        # /page/N path pattern
        if re.search(r"/page/\d+", parsed.path):
            new_path = re.sub(r"/page/\d+", f"/page/{next_page_num}", parsed.path)
            return urllib.parse.urlunparse(parsed._replace(path=new_path))

        logger.warning("[Batch] Cannot determine next page URL from: %s", current_url)
        return None
