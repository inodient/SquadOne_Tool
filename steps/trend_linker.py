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
    alpha: float = 0.3,
    beta: float = 0.7,
    theta_near: float = 0.35,
    theta_gap: float = 0.45,
    max_gap_weeks: int = 30,
    min_ent_jaccard: float = 0.3,
    min_ent_jaccard_near: float = 0.15,
) -> Tuple[List[ThreadRow], List[MetaRow]]:
    """주 간 트렌드를 thread 로 연결. clusters: dict(week·cluster_id·weight·label·top_keywords·centroid).

    시간순 1-패스 greedy: 각 트렌드를 점수 최고의 active thread 에 붙이거나(없으면) 새 thread.
    thread 당 한 주에 1 트렌드. gap=1 은 theta_near, gap>1 은 theta_gap + 엔티티 자카드 게이트.

    성능: 키워드 역색인(keyword→active thread id)으로 키워드를 공유하는 후보 thread 만 비교.
    두 게이트(min_ent_jaccard_near/min_ent_jaccard)가 모두 >0 이면 자카드>0(=키워드 1개 이상 공유)
    이 매칭의 필요조건이라, 역색인 후보만 봐도 전수 비교와 **동일 결과**(후보는 id 오름차순 정렬해
    동률 tie-break 도 보존). 임계값이 0 이하면 키워드 0공유도 통과할 수 있어 전수 스캔으로 폴백.
    """
    by_week: Dict[str, List[Dict[str, object]]] = {}
    for c in clusters:
        by_week.setdefault(str(c["week"]), []).append(c)
    weeks = sorted(by_week.keys(), key=_week_date)

    threads: List[Dict[str, object]] = []          # 출력용 전체 thread(시간순=id순)
    next_id = 0
    active: Dict[int, Dict[str, object]] = {}       # id → 아직 매칭 가능한 thread
    kw_index: Dict[str, set] = {}                    # keyword → active thread id 집합
    # 두 게이트가 모두 양수일 때만 역색인 한정이 전수 비교와 동치(자카드>0 필요조건).
    gate_safe = min_ent_jaccard_near > 0 and min_ent_jaccard > 0

    def _register(th: Dict[str, object]) -> None:
        active[int(th["id"])] = th
        for k in th["entities"]:
            kw_index.setdefault(k, set()).add(int(th["id"]))

    def _add_keywords(th: Dict[str, object], new_kws: set) -> None:
        for k in new_kws:
            kw_index.setdefault(k, set()).add(int(th["id"]))

    def _deactivate(th: Dict[str, object]) -> None:
        tid = int(th["id"])
        active.pop(tid, None)
        for k in th["entities"]:
            s = kw_index.get(k)
            if s is not None:
                s.discard(tid)
                if not s:
                    kw_index.pop(k, None)

    for wk in weeks:
        # gap 은 주차가 진행할수록 단조 증가 → max 초과 thread 는 영구 비활성. 매주 시작 시 제거.
        for th in list(active.values()):
            if _gap_weeks(str(th["last_week"]), wk) > max_gap_weeks:
                _deactivate(th)
        used: set = set()
        items = sorted(by_week[wk], key=lambda c: c["weight"], reverse=True)
        for c in items:
            cent = c["centroid"]
            ents = _ents(str(c.get("top_keywords", "")))
            if gate_safe:
                cand_ids: set = set()
                for k in ents:
                    cand_ids |= kw_index.get(k, set())
                candidates = [active[i] for i in sorted(cand_ids) if i not in used]
            else:
                candidates = [active[i] for i in sorted(active) if i not in used]
            best = None
            best_score = -1.0
            best_type = None
            for th in candidates:
                g = _gap_weeks(str(th["last_week"]), wk)
                if g < 1 or g > max_gap_weeks:
                    continue
                jac = _jaccard(ents, th["entities"])
                # 키워드 공유 게이트 — centroid 만으로 무관한 트렌드를 잇지 않게(오연결 차단).
                if g == 1:
                    thr = theta_near
                    if jac < min_ent_jaccard_near:
                        continue  # 인접 주도 최소한의 키워드 공유 필요
                else:
                    thr = theta_gap
                    if jac < min_ent_jaccard:
                        continue  # 장기 gap 은 엔티티 공유가 강해야 연결(B)
                sc = alpha * _cos(cent, th["centroid"]) + beta * jac
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
                new_kws = ents - best["entities"]
                best["entities"] |= ents
                best["last_week"] = wk
                _add_keywords(best, new_kws)      # 새 키워드 역색인 반영
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
                _register(th)                    # 역색인에 신규 thread 등록
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
        alpha=float(cfg.get("alpha", 0.3)),
        beta=float(cfg.get("beta", 0.7)),
        theta_near=float(cfg.get("theta_near", 0.35)),
        theta_gap=float(cfg.get("theta_gap", 0.45)),
        max_gap_weeks=int(cfg.get("max_gap_weeks", 30)),
        min_ent_jaccard=float(cfg.get("min_ent_jaccard", 0.3)),
        min_ent_jaccard_near=float(cfg.get("min_ent_jaccard_near", 0.15)),
    )
    repo.replace_trend_threads(week_from, week_to, thread_rows, meta_rows)
    n_evolve = sum(1 for m in meta_rows if m[5] == "진화")
    logger.info(
        "trend_linker | %s~%s | 트렌드=%d | thread=%d (진화=%d) | 멤버=%d",
        week_from, week_to, len(clusters), len(meta_rows), n_evolve, len(thread_rows),
    )
    return len(meta_rows)
