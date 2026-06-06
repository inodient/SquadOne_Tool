"""Stage5B-5 — 군집 시계열(부피·강도·활성) (trend_time_series_builder).

해당 주차 cluster_keywords 멤버의 빈도 합(cluster_volume)·평균 z(cluster_intensity)를
집계해 newstrend.trend_ts_cluster 에 적재. 출처: SquadOne_Tool_NewsTrend/steps/
trend_time_series_builder.py (per-week 스냅샷으로 적응).

NOTE: 현행 군집 id 는 주차별 HDBSCAN 라벨이라 주차 간 안정적이지 않다. 따라서 stop_tracking
(연속 N주 강도 미달)은 주차 스냅샷에서 산정하지 않고 is_active(강도≥임계)만 표기한다.
주차 간 군집 추적은 P6에서 군집 매칭 도입 시 확장.
"""

from __future__ import annotations

from typing import Any, Dict

import numpy as np

from db import repository as repo
from steps.common import get_logger, load_config

logger = get_logger("trend_time_series_builder")


def _cfg() -> dict:
    return load_config().get("trend_time_series", {})


def run_trend_time_series_builder(week: str) -> Dict[str, Any]:
    cfg = _cfg()
    if not cfg.get("enabled", True):
        logger.info("trend_time_series 비활성화 — skip")
        return {"week": week, "rows": 0, "skipped": True}

    ck = repo.read_cluster_keywords(week)
    if ck.empty:
        logger.warning("[%s] cluster_keywords 없음 — trend_ts_cluster skip", week)
        return {"week": week, "rows": 0}

    freq = repo.read_weekly_freq(start_week=week, end_week=week)
    freq_map = {str(k): int(c) for k, c in zip(freq["keyword"], freq["count"])} if not freq.empty else {}

    survival = float(cfg.get("survival_threshold", 0.5))
    rows = []
    for cid, grp in ck.groupby("cluster_id"):
        kws = grp["keyword"].tolist()
        volume = float(sum(freq_map.get(k, 0) for k in kws))
        zs = grp["z_score"].astype(float).to_numpy()
        intensity = float(np.mean(zs)) if zs.size else 0.0
        is_active = intensity >= survival
        rows.append((int(cid), volume, intensity, len(kws), is_active, False))

    n = repo.write_trend_ts_cluster(week, rows)
    logger.info("[%s] trend_ts_cluster %d개 군집 적재", week, n)
    return {"week": week, "rows": n}


def main() -> None:
    import argparse
    import json

    p = argparse.ArgumentParser(description="군집 시계열(Stage5B-5)")
    p.add_argument("--week", required=True)
    args = p.parse_args()
    print(json.dumps(run_trend_time_series_builder(args.week), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
