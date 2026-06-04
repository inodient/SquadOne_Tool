"""계절성(연도간 동일주차 재현) 신호 분석 — 임계값 근거용 1회성 스크립트.

z_score_keywords 의 z>=2.0 스파이크만 사용.
키워드별로 '같은 ISO 주차(±1주)에 서로 다른 해에 몇 번 스파이크했는가'를 센다.
 - max_seasonal_years : 가장 강한 주차 클러스터에서 스파이크한 '서로 다른 연도 수'
 - years_observed     : 스파이크가 관측된 전체 연도 수
 - seasonal_ratio     : max_seasonal_years / years_observed
순수 계절어일수록 소수 주차에 여러 해 반복 → max_seasonal_years 큼.
이벤트성 트렌드는 특정 해에만 몰림 → max_seasonal_years 작음.
"""
from __future__ import annotations

import re
from collections import defaultdict

from db.engine import get_conn

WK = re.compile(r"(\d{4})-W(\d{2})")


def parse(week: str):
    m = WK.match(week)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def main() -> None:
    with get_conn() as conn, conn.cursor(name="seas") as cur:
        cur.itersize = 50000
        cur.execute("SELECT keyword, week FROM z_score_keywords WHERE z_score >= 2.0")
        spikes: dict[str, set] = defaultdict(set)  # keyword -> {(year, wk)}
        for kw, wk in cur:
            p = parse(wk)
            if p:
                spikes[kw].add(p)

    rows = []
    for kw, yset in spikes.items():
        years_all = {y for (y, _w) in yset}
        years_observed = len(years_all)
        wknums = {w for (_y, w) in yset}
        # 각 주차 w 에 대해, [w-1,w,w+1] 에 스파이크한 서로 다른 연도 수
        max_seasonal = 0
        best_wk = None
        for w in wknums:
            yrs = {y for (y, ww) in yset if abs(ww - w) <= 1}
            if len(yrs) > max_seasonal:
                max_seasonal = len(yrs)
                best_wk = w
        ratio = max_seasonal / years_observed if years_observed else 0.0
        rows.append((kw, max_seasonal, years_observed, ratio, best_wk,
                     len(yset), len(wknums)))

    n = len(rows)
    print(f"=== z>=2.0 스파이크 보유 키워드: {n:,} ===\n")

    print("=== max_seasonal_years(같은주차±1, 서로 다른 해 반복) 분포 ===")
    for thr in (2, 3, 4, 5, 6, 7, 8):
        c = sum(1 for r in rows if r[1] >= thr)
        print(f"  max_seasonal_years >= {thr}: {c:>6,}개 ({c/n*100:5.2f}%)")
    print()

    print("=== ratio>=0.6 AND max_seasonal_years>=N 교차 (정밀도 강화) ===")
    for thr in (2, 3, 4):
        c = sum(1 for r in rows if r[1] >= thr and r[3] >= 0.6)
        print(f"  seasonal>={thr} & ratio>=0.6: {c:>6,}개 ({c/n*100:5.2f}%)")
    print()

    idx = {r[0]: r for r in rows}

    def show(title, kws):
        print(f"=== {title} ===")
        print(f"  {'keyword':<16}{'seas_yrs':>9}{'yrs_obs':>8}{'ratio':>7}{'best_wk':>8}{'n_spk':>7}{'n_wk':>6}")
        for kw in kws:
            if kw in idx:
                _, ms, yo, ra, bw, nsp, nwk = idx[kw]
                print(f"  {kw:<16}{ms:>9}{yo:>8}{ra:>7.2f}{str(bw):>8}{nsp:>7}{nwk:>6}")
            else:
                print(f"  {kw:<16}{'(z>=2.0 스파이크 없음)':>9}")
        print()

    show("검증: 순수 계절어(제거 기대)",
         ["소띠", "쥐띠", "돼지띠", "김장철", "송년", "가을철", "겨울나기", "서머", "신년", "새해", "추석", "설날"])
    show("검증: 상품성 계절어(라벨은 달되 review로 보존 기대)",
         ["패딩", "빙수", "김장", "수능", "어버이날", "빼빼로", "졸업", "입학", "피서", "단풍"])
    show("검증: 이벤트성 실트렌드(계절 아님 → 안 잡혀야)",
         ["언택트", "캐즘", "오염수", "트루스소셜", "코로나", "마스크", "비트코인", "챗지피티", "당권파", "정개특위"])

    print("=== max_seasonal_years 상위 40 (자동 계절어 후보) ===")
    print(f"  {'keyword':<16}{'seas_yrs':>9}{'yrs_obs':>8}{'ratio':>7}{'best_wk':>8}{'n_wk':>6}")
    for r in sorted(rows, key=lambda x: (x[1], x[3]), reverse=True)[:40]:
        kw, ms, yo, ra, bw, nsp, nwk = r
        print(f"  {kw:<16}{ms:>9}{yo:>8}{ra:>7.2f}{str(bw):>8}{nwk:>6}")


if __name__ == "__main__":
    main()
