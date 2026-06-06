"""Stage5B-4 — 키워드 장기 지속성 신호 (long_term_trend_bridge).

as_of_week 기준 window_weeks(기본 156=3년) 동안의 주차별 z-score 추이로
active_ratio·mean/median/p75 z·추세 기울기·연속 활성·peak·종합 long_term_score 를 계산해
newstrend.long_term_signals 에 적재. 출처: SquadOne_Tool_NewsTrend/steps/long_term_trend_bridge.py.

후보 키워드: 해당 주차 cluster_keywords ∪ 고z 키워드. z 이력은 DB(z_score_keywords).
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from db import repository as repo
from steps.common import get_logger, load_config

logger = get_logger("long_term_trend_bridge")


def _cfg() -> dict:
    return load_config().get("long_term_trend", {})


def _minmax(s: pd.Series) -> pd.Series:
    lo, hi = float(s.min()), float(s.max())
    if hi <= lo:
        return pd.Series([0.0] * len(s), index=s.index)
    return (s - lo) / (hi - lo)


def _consecutive_tail(active_flags: List[bool]) -> int:
    n = 0
    for v in reversed(active_flags):
        if v:
            n += 1
        else:
            break
    return n


def run_long_term_trend_bridge(as_of_week: str, *, candidate_keywords: Optional[List[str]] = None) -> Dict[str, Any]:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        logger.info("long_term_trend 비활성화 — skip")
        return {"week": as_of_week, "rows": 0, "skipped": True}

    window_weeks = int(cfg.get("window_weeks", 156))

    # 후보 키워드 결정
    if candidate_keywords is None:
        cands: set[str] = set()
        try:
            ck = repo.read_cluster_keywords(as_of_week)
            cands |= set(ck["keyword"].tolist())
        except Exception:  # noqa: BLE001
            pass
        zhi = repo.read_zscore(start_week=as_of_week, end_week=as_of_week)
        thr = float(load_config().get("z_score", {}).get("high_z_score_threshold", 2.0))
        cands |= set(zhi[zhi["z_score"] >= thr]["keyword"].tolist())
        candidate_keywords = sorted(cands)

    if not candidate_keywords:
        logger.warning("[%s] 장기신호 후보 키워드 없음", as_of_week)
        return {"week": as_of_week, "rows": 0}

    hist = repo.read_zscore_history(keywords=candidate_keywords, end_week=as_of_week)
    if hist.empty:
        logger.warning("[%s] z 이력 없음", as_of_week)
        return {"week": as_of_week, "rows": 0}

    # 윈도우 주차(최근 window_weeks). ISO 주차는 사전식=시간순.
    all_weeks = sorted(w for w in hist["week"].unique() if w <= as_of_week)
    window = all_weeks[-window_weeks:]
    wset = set(window)
    hist = hist[hist["week"].isin(wset)]

    # 키워드 × 주차 z 매트릭스(결측=0)
    pivot = hist.pivot_table(index="keyword", columns="week", values="z_score", aggfunc="max")
    pivot = pivot.reindex(columns=window).fillna(0.0)

    recs: List[Dict[str, Any]] = []
    x = np.arange(len(window), dtype=float)
    for kw, row in pivot.iterrows():
        z = row.to_numpy(dtype=float)
        active = z > 0
        active_weeks = int(active.sum())
        active_ratio = active_weeks / max(1, len(window))
        nz = z[active] if active_weeks else z
        mean_z = float(np.mean(nz)) if nz.size else 0.0
        median_z = float(np.median(nz)) if nz.size else 0.0
        p75_z = float(np.percentile(nz, 75)) if nz.size else 0.0
        slope = float(np.polyfit(x, z, 1)[0]) if len(window) >= 2 else 0.0
        consec = _consecutive_tail(active.tolist())
        peak = float(np.max(z)) if z.size else 0.0
        recs.append({
            "keyword": kw, "window_weeks": len(window), "active_weeks": active_weeks,
            "active_ratio": active_ratio, "mean_z": mean_z, "median_z": median_z, "p75_z": p75_z,
            "slope_z_per_week": slope, "latest_consecutive_active_weeks": consec, "peak_week_z": peak,
        })
    df = pd.DataFrame(recs)

    # 종합 점수(가중 + min-max 정규화)
    w = cfg.get("score_weights", {})
    n_mean = _minmax(df["mean_z"])
    n_p75 = _minmax(df["p75_z"])
    n_slope = _minmax(df["slope_z_per_week"])
    n_consec = _minmax(df["latest_consecutive_active_weeks"].astype(float))
    df["long_term_score"] = (
        float(w.get("active_ratio", 0.35)) * df["active_ratio"]
        + float(w.get("mean_z", 0.2)) * n_mean
        + float(w.get("p75_z", 0.15)) * n_p75
        + float(w.get("slope", 0.15)) * n_slope
        + float(w.get("consecutive", 0.15)) * n_consec
    )

    df["passes_thresholds"] = (
        (df["active_ratio"] >= float(cfg.get("min_active_ratio", 0.2)))
        & (df["active_weeks"] >= int(cfg.get("min_active_weeks", 36)))
        & (df["mean_z"] >= float(cfg.get("min_mean_z", 0.15)))
        & (df["p75_z"] >= float(cfg.get("min_p75_z", 0.5)))
        & (df["slope_z_per_week"] >= float(cfg.get("min_slope", -0.002)))
        & (df["long_term_score"] >= float(cfg.get("min_long_term_score", 0.55)))
    )

    spike_z = float(cfg.get("spike_z_fallback", 2.5))
    top_n = int(cfg.get("top_n_per_week", 80))
    df = df.sort_values("long_term_score", ascending=False).reset_index(drop=True)
    selected = df["passes_thresholds"] | (df["peak_week_z"] >= spike_z)
    # 상위 top_n 으로 추적 대상 제한
    sel_idx = df.index[selected][:top_n]
    df["selected_for_tracking"] = df.index.isin(sel_idx)

    n = repo.write_long_term_signals(as_of_week, df)
    logger.info("[%s] long_term_signals %d행 (passes=%d, tracked=%d)",
                as_of_week, n, int(df["passes_thresholds"].sum()), int(df["selected_for_tracking"].sum()))
    return {"week": as_of_week, "rows": n, "tracked": int(df["selected_for_tracking"].sum())}


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="장기 지속성 신호(Stage5B-4)")
    p.add_argument("--week", required=True)
    args = p.parse_args()
    print(json.dumps(run_long_term_trend_bridge(args.week), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
