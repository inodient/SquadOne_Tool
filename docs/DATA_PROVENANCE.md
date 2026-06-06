# 데이터 출처 기록 (Data Provenance)

> **목적**: `newstrend` DB(소스 오브 트루스)의 1~4단계 데이터가 **언제·어디서·어떻게** 적재됐는지 검증한 결과를 영구 기록한다.
> **검증일**: 2026-06-02 · **검증자**: Chang Ho Kang (with Claude)
> 이 문서는 "DB에는 537주가 있는데 `weekly_keywords.csv`는 1주뿐"이라는 불일치를 추적·해소한 기록이다.

---

## 1. 결론 (요약)

- **소스 오브 트루스 = PostgreSQL `newstrend` 스키마.** 무결성 확인됨.
- 1~4단계 모두 **2015-W53 ~ 2026-W15 / 537주 연속**(빈 주 0), 정합성 검사 통과.
- DB의 `weekly_keywords`는 구 `SquadOne_NewsTrend` 프로젝트의 **전체 산출물 CSV를 일괄 백필**한 것이며, 백업 CSV와 **행 단위(+1행)까지 일치**한다.
- 디스크의 `data/output/weekly_keywords.csv`(1주, 2023-W13)는 **스모크 테스트 잔재**일 뿐 DB 정합성과 무관하다.

---

## 2. 적재 타임라인 (검증된 사실)

| 시각 (KST) | 사건 | 근거 |
| --- | --- | --- |
| (구 프로젝트) | `SquadOne_NewsTrend`가 537주 전체 산출물 생성 | output meta `csv_path`가 `/SquadOne_NewsTrend/...` |
| 2026-06-01 16:11 | 커밋 `P0: DB-ification 인프라·계약 구축` | git |
| **2026-06-01 15:29** | **전체 백필**: 구 CSV 536주분 → `newstrend.weekly_keywords` 일괄 적재 (39,758,779행) | DB `updated_at` |
| **2026-06-01 16:30** | **2023-W13 스모크 테스트** (신규 Qdrant→DB 1단계). DB에 81,377행 upsert + 로컬 `weekly_keywords.csv` 1주로 덮어씀 | DB `updated_at` 07:30 UTC, `weekly_keywords.meta.json` |
| 2026-06-01 18:42 | 커밋 `P1~P3: 1~7단계 DB화 + Qdrant 입력/검색 통합` | git |
| **2026-06-02 11:55** | 커밋 `실행·점검 CLI 도구 추가` (`scripts/run_step.py` **최초 생성**) | git |

> **중요**: 실행 CLI(`run_step.py`)는 **6/2에 처음 생성**됐다. 따라서 6/1의 `weekly_keywords.csv` 1주 상태는 **CLI 사용자 재실행의 결과가 아니다.** DB화 개발 중의 단일 주차 스모크 테스트였다.

---

## 3. 검증 증거 (수치)

### 3-1. DB 적재 시각 (`weekly_keywords.updated_at`)

`weekly_keywords` 테이블에는 `updated_at timestamptz` 컬럼이 있어 적재 시점이 남아 있다.

| 적재 시각 (UTC) | 행수 | 정체 |
| --- | --: | --- |
| 2026-06-01 06:29 | 39,758,779 | 전체 일괄 백필 (536주) |
| 2026-06-01 07:30 | 81,377 | 2023-W13 스모크 테스트 |
| **합계** | **39,840,156** | = DB 총행수 |

- 2023-W13 행 전부가 `updated_at = 07:30:38` 단일값 → 그 주만 나중에 재적재(upsert)됨.
- `newstrend.pipeline_runs`는 0행(감사 로그 없음). 적재 추적은 `updated_at`이 근거.

### 3-2. 원본 대조 (DB ↔ 구 백업 CSV)

원본: `/Users/changhokang/Desktop/SquadOne_NewsTrend_Backup/data/output/weekly_keywords.csv` (1.2GB)
(동일본: `/Users/changhokang/Desktop/트랜드분석_2016_2026/output/weekly_keywords.csv`)

| 항목 | 백업 CSV | DB | 차이 |
| --- | --: | --: | --: |
| 총 행수 | 39,840,155 | 39,840,156 | **+1** |
| distinct 주차 | 537 | 537 | 0 |
| 주차 범위 | 2015-W53 ~ 2026-W15 | 동일 | — |
| 2023-W13 행수 | 81,376 | 81,377 | **+1** |

- **전체 +1행 = 2023-W13 +1행**으로 완전히 귀속됨. 스모크 테스트가 2023-W13에 신규 `(week,keyword,source)` 1행을 추가 upsert한 것이 유일한 변화.

### 3-3. 단계별 DB 커버리지 (검증 시점)

| 단계 | 테이블 | 주차 범위 | 주차수 | 빈 주 | 행수 |
| :--: | --- | --- | :--: | :--: | --: |
| 1 | weekly_keywords | 2015-W53 ~ 2026-W15 | 537 | 0 | 39,840,156 |
| 2 | weekly_keyword_freq | 2015-W53 ~ 2026-W15 | 537 | 0 | 9,731,349 |
| 3 | base_calculation | 2015-W53 ~ 2026-W15 | 537 | 0 | 3,109,676 |
| 4 | z_score_keywords | 2015-W53 ~ 2026-W15 | 537 | 0 | 9,747,087 |

정합성(`python -m scripts.db_check --full`): freq↔weekly 합계 일치, z_score 주차 ⊆ weekly → **✅ 통과**.

### 3-4. Qdrant 원본

- 컬렉션 `news_10y_ko_v1`, 포인트 **9,437,094**개, `date` 페이로드는 문자열 `YYYYMMDD`.
- (`date`가 문자열이라 숫자 range 집계 불가. 정밀 min/max는 페이로드 인덱스 필요.)

---

## 4. 알려진 단서/주의점

1. **2023-W13만 혼합 기준** — 다른 536주는 100% 구 엑셀 기반 백필, 2023-W13만 신규 Qdrant 경로로 재계산됨(+1행, count 값 갱신). 트렌드 분석 영향은 미미하나, 추후 1단계를 전 주차 Qdrant 경로로 통일 재계산하면 정합된다.
2. **디스크 output 폴더는 신·구 혼재** — 신뢰 기준은 항상 DB. 특히:
   - `weekly_keywords.csv` = 2023-W13 1주 스모크 잔재 (DB 정본 아님)
   - `base_calculation.csv` / `z_score_keywords.csv` = 구 전체 export(537주). meta는 4월 구버전(`/SquadOne_NewsTrend/`)에서 멈춰 있음
   - `frequency_matrix.csv`, `z_score_keywords_group.csv`, `cluster_*`, `long_term_*` = 구 NewsTrend 레거시
3. **3·4단계 적재 방식 차이** — `base_calculation`/`z_score_keywords` 리포지토리는 `TRUNCATE → COPY`(전체 재계산), `weekly_keywords`는 `INSERT ON CONFLICT`(증분 upsert), `weekly_keyword_freq`는 주차 단위 `DELETE→INSERT`. (`db/repository.py` 참조)

---

## 5. 재현 방법 (재검증 시)

```bash
# DB 커버리지·정합성
python -m scripts.db_check --full

# 적재 시각 분포
python3 -c "from db.engine import get_conn;
import itertools;
c=get_conn(); cur=c.cursor();
cur.execute(\"SELECT date_trunc('minute',updated_at), count(*) FROM newstrend.weekly_keywords GROUP BY 1 ORDER BY 1\");
print(cur.fetchall())"

# 원본 백업 CSV 대조
#   행수/주차: /Users/changhokang/Desktop/SquadOne_NewsTrend_Backup/data/output/weekly_keywords.csv
```
