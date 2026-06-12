"""trend_linker 파라미터 자동 그리드 탐색 — 메모리 내 평가로 빠르게 최고 조합 선택.

군집(weekly_trend_clusters)은 1회 로드, 각 파라미터 조합으로 link_threads 를 메모리에서 돌려
eval_trends 와 동일한 종합 점수를 계산한다(DB 왕복 없음). 최고 조합만 DB 에 적재.
과적합 방지: 종합점수는 coherence(일반) 가중 최대; 앵커는 보조. 다른 주차 holdout 으로 별도 검증 권장.

사용: python -m scripts.tune_linker --from-week 2025-W22 --to-week 2025-W26 [--write]
"""
from __future__ import annotations

import argparse
import itertools

from db import repository as repo
from db.config import load_shared_env
from steps.trend_linker import link_threads

ANCHOR = {
    "내란", "특검", "계엄", "탄핵", "재판", "수사", "기소", "구속",
    "공판", "재판부", "판사", "피고인", "검찰", "윤석열",
}


def _ents(s: str) -> set:
    return {t.strip() for t in (s or "").split(",") if t.strip()}


def _jac(a: set, b: set) -> float:
    u = len(a | b)
    return len(a & b) / u if u else 0.0


def score_linking(thread_rows, clusters) -> dict:
    """eval_trends 와 동일 공식의 인메모리 평가."""
    kmap = {(c["week"], int(c["cluster_id"])): _ents(c["top_keywords"]) for c in clusters}
    wmap = {(c["week"], int(c["cluster_id"])): int(c["weight"]) for c in clusters}
    threads: dict = {}
    for tid, wk, cid, seq, sc, lt in thread_rows:
        threads.setdefault(tid, []).append((seq, kmap.get((wk, int(cid)), set()), wmap.get((wk, int(cid)), 0)))

    co_num = co_den = 0.0
    multi = 0
    single = 0
    pur_list = []
    for ms in threads.values():
        if len(ms) == 1:
            single += 1
        if len(ms) >= 2:
            multi += 1
            ms.sort(key=lambda x: x[0])
            js = [_jac(ms[i][1], ms[i + 1][1]) for i in range(len(ms) - 1)]
            peak = max(m[2] for m in ms)
            co_num += (sum(js) / len(js)) * peak
            co_den += peak
            if any(e & ANCHOR for _, e, _w in ms):
                anchor_members = sum(1 for _, e, _w in ms if e & ANCHOR)
                pur_list.append(anchor_members / len(ms))
    n = len(threads) or 1
    coherence = co_num / co_den if co_den else 0.0
    anchor_purity = sum(pur_list) / len(pur_list) if pur_list else 0.0
    single_rate = single / n
    # label_div 는 군집 산물(파라미터 불변)
    labels = [c.get("label", "") for c in clusters]
    label_div = len(set(labels)) / len(labels) if labels else 0.0
    overall = (
        0.45 * coherence + 0.35 * anchor_purity + 0.20 * label_div
        - max(0.0, single_rate - 0.6) * 0.2
    )
    return {
        "overall": round(overall, 4), "coherence": round(coherence, 4),
        "anchor_purity": round(anchor_purity, 4), "label_div": round(label_div, 4),
        "single_rate": round(single_rate, 4), "multi": multi,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-week", required=True)
    ap.add_argument("--to-week", required=True)
    ap.add_argument("--write", action="store_true", help="최고 조합을 DB 에 적재")
    a = ap.parse_args()
    load_shared_env()

    clusters = repo.get_trend_clusters_range(a.from_week, a.to_week)
    print(f"clusters={len(clusters)}  ({a.from_week}~{a.to_week})")

    # 그리드(과적합 방지 위해 상식 범위로 제한)
    grid = []
    for alpha in (0.2, 0.3, 0.4):
        for theta_near in (0.30, 0.38):
            for jn in (0.12, 0.18, 0.25):
                for jg in (0.30, 0.40):
                    grid.append(dict(
                        alpha=alpha, beta=round(1 - alpha, 2),
                        theta_near=theta_near, theta_gap=max(theta_near + 0.05, 0.40),
                        min_ent_jaccard_near=jn, min_ent_jaccard=jg, max_gap_weeks=30,
                    ))
    print(f"조합 {len(grid)}개 탐색...")
    best = None
    for i, combo in enumerate(grid, 1):
        tr, mr = link_threads(clusters, **combo)
        s = score_linking(tr, clusters)
        if best is None or s["overall"] > best[0]["overall"]:
            best = (s, combo, tr, mr)
        if i % 5 == 0 or s["overall"] == best[0]["overall"]:
            print(f"  [{i:2}/{len(grid)}] overall={s['overall']} coh={s['coherence']} "
                  f"anc={s['anchor_purity']} sgl={s['single_rate']} | a={combo['alpha']} "
                  f"tn={combo['theta_near']} jn={combo['min_ent_jaccard_near']} jg={combo['min_ent_jaccard']}")
    s, combo, tr, mr = best
    print("\n=== BEST ===")
    print("  params:", combo)
    print("  score :", s)
    if a.write:
        repo.replace_trend_threads(a.from_week, a.to_week, tr, mr)
        print(f"  → DB 적재 완료 (thread={len(mr)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
