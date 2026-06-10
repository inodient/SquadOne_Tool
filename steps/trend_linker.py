"""주 간 트렌드 연결(thread) — A 동일 반복 + B 진화(1심→2심→3심)를 한 레이어에서.

입력: weekly_trend_clusters(주차 트렌드 + centroid + top_keywords).
방법: 같은 임베딩 공간이라 centroid 코사인으로 "비슷한 이야기", top_keywords 자카드로 "같은 사건".
  - A 반복/지속: 인접 주(gap=1), centroid 중심.
  - B 진화:      gap 허용(반년~1년), 엔티티(키워드) 공유 중심 → drift 따라가며 연결.
범위 입력(week_from~week_to)이라 1단계 전체 완료 없이 소구간만 테스트 가능.
순수 함수 link_threads 로 DB 없이 단위검증 가능.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Dict, List, Sequence, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# thread 멤버: (thread_id, week, cluster_id, seq, link_score, link_type)
ThreadRow = Tuple[int, str, int, int, float, str]
# thread 요약: (thread_id, n_weeks, start, end, span, kind, drift, peak_weight, label, label_path, top_keywords)
MetaRow = Tuple[int, int, str, str, int, str, float, int, str, str, str]


def _week_date(week: str) -> datetime:
    return datetime.strptime(f"{week}-1", "%G-W%V-%u")


def _gap_weeks(w1: str, w2: str) -> int:
    return abs((_week_date(w2) - _week_date(w1)).days) // 7


def _norm(v) -> np.ndarray:
    a = np.asarray(v, dtype=np.float64)
    n = np.linalg.norm(a)
    return a / n if n else a


def _cos(a, b) -> float:
    if a is None or b is None or len(a) == 0 or len(b) == 0:
        return 0.0
    return float(np.dot(_norm(a), _norm(b)))


def _ents(top_keywords: str) -> set:
    return {t.strip() for t in (top_keywords or "").split(",") if t.strip()}


def _jaccard(s1: set, s2: set) -> float:
    if not s1 or not s2:
        return 0.0
    u = len(s1 | s2)
    return len(s1 & s2) / u if u else 0.0


def link_threads(
    clusters: Sequence[Dict[str, object]],
    *,
    alpha: float = 0.6,
    beta: float = 0.4,
    theta_near: float = 0.55,
    theta_gap: float = 0.6,
    max_gap_weeks: int = 30,
    min_ent_jaccard: float = 0.25,
) -> Tuple[List[ThreadRow], List[MetaRow]]:
    """주 간 트렌드를 thread 로 연결. clusters: dict(week·cluster_id·weight·label·top_keywords·centroid).

    시간순 1-패스 greedy: 각 트렌드를 점수 최고의 active thread 에 붙이거나(없으면) 새 thread.
    thread 당 한 주에 1 트렌드. gap=1 은 theta_near, gap>1 은 theta_gap + 엔티티 자카드 게이트.
    """
    by_week: Dict[str, List[Dict[str, object]]] = {}
    for c in clusters:
        by_week.setdefault(str(c["week"]), []).append(c)
    weeks = sorted(by_week.keys(), key=_week_date)

    threads: List[Dict[str, object]] = []
    next_id = 0

    for wk in weeks:
        used: set = set()
        items = sorted(by_week[wk], key=lambda c: c["weight"], reverse=True)
        for c in items:
            cent = c["centroid"]
            ents = _ents(str(c.get("top_keywords", "")))
            best = None
            best_score = -1.0
            best_type = None
            for th in threads:
                if th["id"] in used:
                    continue
                g = _gap_weeks(str(th["last_week"]), wk)
                if g < 1 or g > max_gap_weeks:
                    continue
                jac = _jaccard(ents, th["entities"])
                sc = alpha * _cos(cent, th["centroid"]) + beta * jac
                if g == 1:
                    thr = theta_near
                else:
                    thr = theta_gap
                    if jac < min_ent_jaccard:
                        continue  # 장기 gap 은 엔티티 공유가 강해야 연결(B)
                if sc >= thr and sc > best_score:
                    best, best_score, best_type = th, sc, ("near" if g == 1 else "gap")
            if best is not None:
                seq = len(best["members"])
                best["members"].append({
                    "week": wk, "cluster_id": int(c["cluster_id"]), "seq": seq,
                    "score": best_score, "type": best_type, "weight": int(c["weight"]),
                    "label": str(c.get("label", "")), "centroid": cent, "ents": ents,
                })
                best["centroid"] = cent          # drift 따라감
                best["entities"] |= ents
                best["last_week"] = wk
                used.add(best["id"])
            else:
                th = {
                    "id": next_id, "last_week": wk, "centroid": cent, "entities": set(ents),
                    "members": [{
                        "week": wk, "cluster_id": int(c["cluster_id"]), "seq": 0,
                        "score": 0.0, "type": "seed", "weight": int(c["weight"]),
                        "label": str(c.get("label", "")), "centroid": cent, "ents": ents,
                    }],
                }
                threads.append(th)
                used.add(next_id)
                next_id += 1

    thread_rows: List[ThreadRow] = []
    meta_rows: List[MetaRow] = []
    for th in threads:
        ms = th["members"]
        for m in ms:
            thread_rows.append(
                (int(th["id"]), m["week"], m["cluster_id"], m["seq"], float(m["score"]), m["type"])
            )
        n_weeks = len(ms)
        start, end = ms[0]["week"], ms[-1]["week"]
        span = _gap_weeks(start, end) + 1
        if n_weeks >= 2:
            cs = [_cos(ms[i]["centroid"], ms[i + 1]["centroid"]) for i in range(n_weeks - 1)]
            drift = float(sum(cs) / len(cs))
        else:
            drift = 1.0
        peak = max(ms, key=lambda m: m["weight"])
        if n_weeks == 1:
            kind = "단발"
        elif drift >= 0.88:
            kind = "반복"
        else:
            kind = "진화"
        label_path = " → ".join(m["label"] or "?" for m in ms)
        topkw = ", ".join(list(th["entities"])[:8])
        meta_rows.append(
            (int(th["id"]), n_weeks, start, end, span, kind, drift,
             int(peak["weight"]), peak["label"] or "", label_path, topkw)
        )
    return thread_rows, meta_rows


def run(week_from: str, week_to: str, config: dict | None = None, logger=logger) -> int:
    """범위 [week_from, week_to] 트렌드를 읽어 thread 연결 후 적재. 반환 thread 수."""
    from db import repository as repo

    cfg = ((config or {}).get("trend_linker") or {}) if config else {}
    clusters = repo.get_trend_clusters_range(week_from, week_to)
    if not clusters:
        logger.info("trend_linker | 범위에 트렌드 없음 | %s~%s", week_from, week_to)
        return 0
    thread_rows, meta_rows = link_threads(
        clusters,
        alpha=float(cfg.get("alpha", 0.6)),
        beta=float(cfg.get("beta", 0.4)),
        theta_near=float(cfg.get("theta_near", 0.55)),
        theta_gap=float(cfg.get("theta_gap", 0.6)),
        max_gap_weeks=int(cfg.get("max_gap_weeks", 30)),
        min_ent_jaccard=float(cfg.get("min_ent_jaccard", 0.25)),
    )
    repo.replace_trend_threads(week_from, week_to, thread_rows, meta_rows)
    n_evolve = sum(1 for m in meta_rows if m[5] == "진화")
    logger.info(
        "trend_linker | %s~%s | 트렌드=%d | thread=%d (진화=%d) | 멤버=%d",
        week_from, week_to, len(clusters), len(meta_rows), n_evolve, len(thread_rows),
    )
    return len(meta_rows)
