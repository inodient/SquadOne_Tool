"""트렌드/연결 품질 평가 — 객관적 종합 점수(과적합 방지용 기준점).

지표(범위 [week_from, week_to]):
  1) coherence   : 다주 thread 의 인접 멤버 키워드 자카드 가중평균. 오연결이면 낮음. (핵심)
  2) anchor_purity: 앵커 사건(내란재판군) 트렌드가 속한 thread 의 앵커 순도 평균. (핵심)
  3) label_div   : 트렌드 라벨 고유성(distinct/total). 라벨 변별력.
  4) single_rate : 단발 thread 비율(참고; 과하면 페널티).
종합 = 0.45*coherence + 0.35*anchor_purity + 0.20*label_div - max(0, single_rate-0.6)*0.2

과적합 방지: 내란(anchor)에만 맞추지 않도록 coherence(일반)에 최대 가중. 앵커는 보조 검증.
사용: python -m scripts.eval_trends --from-week 2025-W22 --to-week 2025-W26 [--json]
"""
from __future__ import annotations

import argparse
import json

from db.config import pg_connect_kwargs
import psycopg

# 앵커 사건군(내란 재판) — 검증용. 과적합 방지 위해 종합점수 가중은 보조(0.35).
ANCHOR = {
    "내란", "특검", "계엄", "탄핵", "재판", "수사", "기소", "구속",
    "공판", "재판부", "판사", "피고인", "검찰", "윤석열",
}


def _ents(s: str) -> set:
    return {t.strip() for t in (s or "").split(",") if t.strip()}


def _jac(a: set, b: set) -> float:
    u = len(a | b)
    return len(a & b) / u if u else 0.0


def evaluate(week_from: str, week_to: str) -> dict:
    kw = pg_connect_kwargs()
    conn = psycopg.connect(kw["conninfo"], options=kw["options"])
    cur = conn.cursor()

    # thread 멤버 + 키워드/weight (범위)
    cur.execute(
        """select t.thread_id, t.seq, c.top_keywords, c.weight
           from newstrend.trend_threads t
           join newstrend.weekly_trend_clusters c on c.week=t.week and c.cluster_id=t.cluster_id
           where t.week between %s and %s order by t.thread_id, t.seq""",
        (week_from, week_to),
    )
    threads: dict = {}
    for tid, seq, kws, w in cur.fetchall():
        threads.setdefault(tid, []).append((seq, _ents(kws), int(w)))

    # 1) coherence: 다주 thread 인접 자카드, peak weight 가중
    co_num = co_den = 0.0
    multi = 0
    for tid, ms in threads.items():
        if len(ms) < 2:
            continue
        multi += 1
        ms.sort(key=lambda x: x[0])
        js = [_jac(ms[i][1], ms[i + 1][1]) for i in range(len(ms) - 1)]
        peak = max(m[2] for m in ms)
        co_num += (sum(js) / len(js)) * peak
        co_den += peak
    coherence = co_num / co_den if co_den else 0.0

    # 단발율
    n_threads = len(threads)
    single_rate = sum(1 for ms in threads.values() if len(ms) == 1) / n_threads if n_threads else 0.0

    # 2) anchor_purity: 앵커 트렌드가 속한 thread 의 앵커 순도
    #    멤버가 앵커 키워드를 하나라도 가지면 앵커 멤버. 앵커 thread = 앵커 멤버 보유 thread.
    pur_list = []
    anchor_threads = 0
    for tid, ms in threads.items():
        anchor_members = sum(1 for _, e, _w in ms if e & ANCHOR)
        if anchor_members == 0:
            continue
        anchor_threads += 1
        if len(ms) >= 2:  # 연결된 것만 순도 평가(단발은 자명히 1.0이라 제외)
            pur_list.append(anchor_members / len(ms))
    anchor_purity = sum(pur_list) / len(pur_list) if pur_list else 0.0

    # 3) label_div: 트렌드 라벨 고유성
    cur.execute(
        "select label from newstrend.weekly_trend_clusters where week between %s and %s",
        (week_from, week_to),
    )
    labels = [r[0] or "" for r in cur.fetchall()]
    label_div = len(set(labels)) / len(labels) if labels else 0.0

    # 4) describability(보조 리포트 지표 — overall 공식엔 미반영).
    #    verb_rate: 라벨에 순수동사(-다 종결)가 든 트렌드 비율 = 동사판 라벨의 추가가치.
    #      (출시·수사 같은 동작명사는 명사판에도 있어 제외; -다 동사만 동사판 고유 기여.)
    #    avg_label_tokens: 라벨 서술 풍부도.
    n_lab = len(labels)
    verb_rate = (
        sum(1 for lab in labels if any(t.endswith("다") and len(t) >= 2 for t in lab.split()))
        / n_lab if n_lab else 0.0
    )
    avg_label_tokens = sum(len(lab.split()) for lab in labels) / n_lab if n_lab else 0.0
    conn.close()

    overall = (
        0.45 * coherence
        + 0.35 * anchor_purity
        + 0.20 * label_div
        - max(0.0, single_rate - 0.6) * 0.2
    )
    return {
        "range": f"{week_from}~{week_to}",
        "threads": n_threads,
        "multi_threads": multi,
        "coherence": round(coherence, 4),
        "anchor_purity": round(anchor_purity, 4),
        "anchor_threads": anchor_threads,
        "label_div": round(label_div, 4),
        "single_rate": round(single_rate, 4),
        "overall": round(overall, 4),
        # describability 보조지표(overall 미반영) — 라벨 정책 모니터링용.
        "verb_rate": round(verb_rate, 4),
        "avg_label_tokens": round(avg_label_tokens, 4),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-week", required=True)
    ap.add_argument("--to-week", required=True)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    from db.config import load_shared_env
    load_shared_env()
    r = evaluate(a.from_week, a.to_week)
    if a.json:
        print(json.dumps(r, ensure_ascii=False))
    else:
        print(f"=== 트렌드 품질 평가 {r['range']} ===")
        print(f"  thread {r['threads']} (다주 {r['multi_threads']}, 앵커 {r['anchor_threads']})")
        print(f"  coherence    : {r['coherence']}   (인접 키워드 자카드, 핵심)")
        print(f"  anchor_purity: {r['anchor_purity']}   (내란 thread 순도)")
        print(f"  label_div    : {r['label_div']}   (라벨 고유성)")
        print(f"  single_rate  : {r['single_rate']}")
        print(f"  ── 종합 overall: {r['overall']} ──")
        print(f"  [describability 보조] verb_rate: {r['verb_rate']}  "
              f"avg_label_tokens: {r['avg_label_tokens']}   (overall 미반영)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
