"""Stage5B-6 — 미시/거시/계절/모멘텀 신호 (period_trend).

주차 빈도 시계열(weekly_keyword_freq)로 키워드별:
- micro: 직전 주 대비 급등(WoW%≥임계)
- macro: 최근 ~13주 회귀 기울기>0 & R²≥임계 (안정 상승추세)
- seasonal: 52주 전 동주 대비 비율
- momentum: 30주 베이스라인 z
를 계산해 newstrend.period_trend_signals 에 적재.
출처: SquadOne_Tool_Prototype/agents/period_trend (일간→주간 적응).
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from db import repository as repo
from steps.common import get_logger, load_config

logger = get_logger("period_trend")


def _cfg() -> dict:
    return load_config().get("period_trend", {})


def _r2(x: np.ndarray, y: np.ndarray, slope: float, intercept: float) -> float:
    pred = slope * x + intercept
    ss_res = float(np.sum((y - pred) ** 2))
    ss_tot = float(np.sum((y - np.mean(y)) ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0


def run_period_trend(as_of_week: str, *, candidate_keywords: Optional[List[str]] = None) -> Dict[str, Any]:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        logger.info("period_trend 비활성화 — skip")
        return {"week": as_of_week, "rows": 0, "skipped": True}

    top_k = int(cfg.get("top_keywords", 40))
    macro_weeks = max(4, int(math.ceil(int(cfg.get("macro_window_days", 90)) / 7)))
    micro_pct = float(cfg.get("micro_spike_pct", 200.0))
    macro_min_beta = float(cfg.get("macro_min_beta", 0.0))
    macro_min_r2 = float(cfg.get("macro_min_r2", 0.7))
    z_strong = float(cfg.get("momentum_z_strong", 2.0))

    # 후보: 7단계 군집 키워드(cluster_keywords) ∪ 해당 주차 고z 상위
    # → 결합 뷰(v_keyword_enriched) JOIN 누락 방지를 위해 7단계 키워드 집합을 포함한다.
    if candidate_keywords is None:
        cands: set[str] = set()
        try:
            ck = repo.read_cluster_keywords(as_of_week)
            cands |= set(ck["keyword"].tolist())
        except Exception:  # noqa: BLE001
            pass
        zhi = repo.read_zscore(start_week=as_of_week, end_week=as_of_week)
        cands |= set(zhi.sort_values("z_score", ascending=False).head(top_k)["keyword"].tolist())
        try:
            cands -= repo.list_excluded_keywords()
        except Exception:  # noqa: BLE001
            pass
        candidate_keywords = sorted(cands)
    if not candidate_keywords:
        logger.warning("[%s] period_trend 후보 없음", as_of_week)
        return {"week": as_of_week, "rows": 0}

    # 빈도 이력
    freq = repo.read_weekly_freq()
    freq = freq[freq["keyword"].isin(candidate_keywords) & (freq["week"] <= as_of_week)]
    if freq.empty:
        logger.warning("[%s] period_trend 빈도 이력 없음", as_of_week)
        return {"week": as_of_week, "rows": 0}

    weeks_sorted = sorted(freq["week"].unique())
    pivot = freq.pivot_table(index="keyword", columns="week", values="count", aggfunc="sum").fillna(0.0)
    pivot = pivot.reindex(columns=weeks_sorted).fillna(0.0)

    recs: List[Dict[str, Any]] = []
    for kw, row in pivot.iterrows():
        series = row.to_numpy(dtype=float)
        cur = float(series[-1])
        prev = float(series[-2]) if len(series) >= 2 else 0.0
        wow = ((cur - prev) / prev * 100.0) if prev > 0 else (100.0 if cur > 0 else 0.0)
        micro = wow >= micro_pct

        # macro: 최근 macro_weeks 회귀
        tail = series[-macro_weeks:]
        if len(tail) >= 4:
            x = np.arange(len(tail), dtype=float)
            slope, intercept = np.polyfit(x, tail, 1)
            r2 = _r2(x, tail, float(slope), float(intercept))
        else:
            slope, r2 = 0.0, 0.0
        macro_up = (slope > macro_min_beta) and (r2 >= macro_min_r2)

        # seasonal: 52주 전 동주 대비
        seasonal = float("nan")
        if len(series) > 52 and series[-53] > 0:
            seasonal = cur / float(series[-53])

        # momentum: 30주 베이스라인 z
        base = series[-31:-1] if len(series) >= 31 else series[:-1]
        if base.size >= 2 and np.std(base) > 0:
            z30 = (cur - float(np.mean(base))) / float(np.std(base))
        else:
            z30 = 0.0

        # 종합 라벨
        strong = z30 >= z_strong
        if micro and macro_up:
            label = "mixed"
        elif micro or strong:
            label = "short"
        elif macro_up:
            label = "long"
        else:
            label = "neutral"

        recs.append({
            "keyword": kw, "point_mentions": cur, "z_score_30d_baseline": z30,
            "delta_1d_pct": float("nan"), "wow_7d_pct": wow, "micro_spike_rule": bool(micro),
            "macro_beta_ma7_90d": float(slope), "macro_r2_90d": float(r2),
            "macro_stable_uptrend": bool(macro_up), "seasonal_ratio": seasonal,
            "window_primary_label": label,
            "window_tags": {"micro": bool(micro), "macro_up": bool(macro_up), "momentum_strong": bool(strong)},
        })

    df = pd.DataFrame(recs)
    n = repo.write_period_trend_signals(as_of_week, df)
    logger.info("[%s] period_trend_signals %d행 적재", as_of_week, n)
    return {"week": as_of_week, "rows": n}


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="주기 신호(Stage5B-6)")
    p.add_argument("--week", required=True)
    args = p.parse_args()
    print(json.dumps(run_period_trend(args.week), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
