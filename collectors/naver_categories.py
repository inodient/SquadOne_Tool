#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stage7d 참조데이터 — 네이버 데이터랩 쇼핑인사이트 카테고리 분류체계 수집.

getCategory.naver API를 BFS로 순회해 1~4분류 리프 목록을 만들고
newstrend.naver_categories 테이블에 적재한다(상품 후보 그라운딩용).
출처: SquadOne_Tool_Naver_Trend_수집/fetch_shopping_categories.py (stdlib only).

CLI:
    python -m collectors.naver_categories            # 수집 → DB 적재
    python -m collectors.naver_categories --csv out.csv   # CSV 로도 저장
    python -m collectors.naver_categories --no-db    # DB 적재 생략
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from typing import Any, Dict, List, Tuple, Union

BASE = "https://datalab.naver.com/shoppingInsight/getCategory.naver"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Referer": "https://datalab.naver.com/shoppingInsight/sCategory.naver",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
}


def fetch_category(cid: int, cache: Dict[int, Dict[str, Any]], base_delay: float, jitter: float) -> Dict[str, Any]:
    if cid in cache:
        return cache[cid]
    req = urllib.request.Request(f"{BASE}?cid={cid}", headers=HEADERS)
    delay = base_delay
    for attempt in range(12):
        try:
            time.sleep(delay + random.uniform(0, jitter))
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            cache[cid] = data
            if len(cache) % 25 == 0:
                print(f"… API {len(cache)}회", flush=True, file=sys.stderr)
            return data
        except urllib.error.HTTPError as e:
            if e.code == 429:
                wait = min(90.0, 5.0 * (attempt + 1) + random.random() * 3)
                print(f"429 cid={cid}, {wait:.0f}s 대기 후 재시도", flush=True, file=sys.stderr)
                time.sleep(wait)
                delay = min(delay + 0.08, 0.9)
                continue
            raise
    raise RuntimeError(f"getCategory 실패: cid={cid}")


def _pad4(names: List[str], cids: List[int]) -> Tuple[List[str], List[Union[int, str]]]:
    names = (names + ["", "", "", ""])[:4]
    cids4 = (list(cids) + ["", "", "", ""])[:4]
    return names, cids4


def fetch_categories(base_delay: float = 0.38, jitter: float = 0.12) -> List[dict]:
    """전체 분류체계를 BFS로 수집해 리프 행 리스트 반환."""
    cache: Dict[int, Dict[str, Any]] = {}
    rows_out: List[dict] = []
    root = fetch_category(0, cache, base_delay, jitter)
    q: deque[Tuple[int, List[str], List[int]]] = deque()
    queued: set[int] = set()
    for ch in sorted(root.get("childList") or [], key=lambda x: (x.get("expsOrder") or 0, x.get("name") or "")):
        if ch.get("deleted"):
            continue
        cid0 = int(ch["cid"])
        if cid0 not in queued:
            queued.add(cid0)
            q.append((cid0, [], []))

    while q:
        cid, path_names, path_cids = q.popleft()
        node = fetch_category(cid, cache, base_delay, jitter)
        name = (node.get("name") or "").strip()
        children = [c for c in (node.get("childList") or []) if not c.get("deleted")]
        leaf = bool(node.get("leaf"))
        full_names = path_names + ([name] if name else [])
        full_cids = path_cids + ([cid] if cid else [])
        if leaf or not children:
            n4, c4 = _pad4(full_names, full_cids)
            rows_out.append({
                "cat1": n4[0], "cat2": n4[1], "cat3": n4[2], "cat4": n4[3],
                "cid": str(cid),
                "level": int(node.get("level") or max(1, len([x for x in full_names if x]))),
                "leaf": bool(leaf),
                "name": (full_names[-1] if full_names else ""),
                "full_path": (node.get("fullPath") or "").strip() or " > ".join(x for x in full_names if x),
            })
            continue
        for ch in sorted(children, key=lambda x: (x.get("expsOrder") or 0, x.get("name") or "")):
            ncid = int(ch["cid"])
            if ncid in queued:
                continue
            queued.add(ncid)
            q.append((ncid, full_names, full_cids))
    return rows_out


def load_to_db(rows: List[dict]) -> int:
    """수집 행을 newstrend.naver_categories 에 전량 교체 적재."""
    from db import repository as repo

    db_rows = [
        (r["cid"], r["level"], r["name"], r["full_path"],
         r["cat1"], r["cat2"], r["cat3"], r["cat4"], r["leaf"])
        for r in rows if r.get("cid")
    ]
    return repo.replace_naver_categories(db_rows)


def write_csv(rows: List[dict], out_path: str) -> None:
    fieldnames = ["cid", "level", "leaf", "name", "full_path", "cat1", "cat2", "cat3", "cat4"]
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in fieldnames})


def main() -> None:
    p = argparse.ArgumentParser(description="네이버 쇼핑 카테고리 분류체계 수집 → DB 적재")
    p.add_argument("--csv", default=None, help="CSV 로도 저장할 경로")
    p.add_argument("--no-db", action="store_true", help="DB 적재 생략")
    p.add_argument("--base-delay", type=float, default=0.38)
    p.add_argument("--jitter", type=float, default=0.12)
    args = p.parse_args()

    rows = fetch_categories(base_delay=args.base_delay, jitter=args.jitter)
    print(f"수집 완료: {len(rows)}개 리프 카테고리", flush=True, file=sys.stderr)
    if args.csv:
        write_csv(rows, args.csv)
        print(f"CSV 저장: {args.csv}", flush=True, file=sys.stderr)
    if not args.no_db:
        n = load_to_db(rows)
        print(f"DB 적재: newstrend.naver_categories {n}행", flush=True, file=sys.stderr)


if __name__ == "__main__":
    main()
