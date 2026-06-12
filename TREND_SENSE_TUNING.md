# 트렌드 sense 튜닝 정리 (2026-06-11)

뉴스 1단계 키워드 "의미분화(sense)"를 **문장 기반 전역 트렌드 군집**으로 재설계하고,
그 위에 **trend_linker(주 간 연결)**를 얹어 트렌드를 추적·고도화한 작업의 전체 기록.

---

## 1. 목표와 평가 기준

- **목표**: 그 주의 뉴스를 "설명 가능한 트렌드(말묶음)"로 군집하고, 주 간으로 연결(반복/진화) 추적.
- **평가식** (`scripts/eval_trends.py`):
  ```
  overall = 0.45·coherence + 0.35·anchor_purity + 0.20·label_div − max(0, single_rate−0.6)·0.2
  ```
  - `coherence` : thread 인접 주차의 키워드 자카드(핵심 일관성)
  - `anchor_purity` : 내란 재판 사건어(내란·특검·재판·윤석열 등) thread 순도 ※2025-W22~W26 특정 사건 기준
  - `label_div` : 라벨 고유성
  - `single_rate` : 단발(1주) thread 비율 (0.6 초과분 페널티)
  - **목표 0.65** (coherence≥0.5 / anchor≥0.8 / label_div≥0.55)
- **테스트 범위**: 2025-W22~W26 (5주), 검증 사건 = 내란 재판.

---

## 2. 점수 진행 (요약)

| 단계 | 핵심 변경 | overall | 비고 |
|---|---|---|---|
| 시작(이전 세션) | linker 키워드 게이트 | 0.459 | |
| 인물 포함 | NNP(윤석열·이재명) 채택 후 재군집 | **0.526** | |
| 군집 K 최적화 | 트렌드 1200→~100개/주 | 0.592 | 과granular 해소 |
| K+linker 동시튜닝 | alpha0.15 등 | 0.635 | |
| top-2 라벨 | 라벨=상위 2개 변별 키워드 | **0.712** | label_div 0.44→0.86 |
| 노이즈 정제 | 바이라인 필터 + 노이즈 33단어 | 0.720 | 정치일반어 제거가 결정타(anchor 0.76→0.97) |
| VA 형용사 제외 | 다-종결 형용사 제거 | **0.744** | anchor 1.0 |
| 동사 추출 + 액션 라벨 | VV동사 + [엔티티]+[액션] 라벨 | 0.685 | **describability↑**(점수는 메트릭 한계로↓) |
| 대표문장 수정 | rep = centroid 최근접 | 0.685 | 표시 정확도↑(점수 불변) |

> **최종 0.6848** (coherence 0.536 / anchor 0.847 / label_div 0.822 / single 0.687)

---

## 3. 단계별 상세

### (1) 인물 포함 — NNP 채택
- Kiwi(sbg)는 NER을 주지 않음(`token.ner=None`) → 기존 `_is_person_entity`가 항상 False라 윤석열·이재명(NNP) 누락.
- **수정**: NNP 태그로 인물 판별, `include_person`일 때 채택. weekly_keywords는 불변(인물은 sense 경로만).

### (2) 군집 입도 K — 가장 큰 레버
- 기존 `cluster_kmax=1200` → 고유문장 ~67k를 과도하게 잘게 쪼갬 → single_rate 0.815(81% 단발).
- **~100~110 트렌드/주**로 굵게 → 트렌드가 주 간 재현 → coherence·single 동반 개선.
- 최종 `cluster_target_size=135, kmin=10, kmax=110`.

### (3) top-2 라벨
- 기존 top-1("내란")은 여러 다른 트렌드에 중복 → 변별력↓.
- top-2 조합("내란 재판"/"내란 특검")으로 서술성·고유성↑.

### (4) 노이즈 정제
- **바이라인/정형구 문장 필터**(`_is_byline_or_boilerplate`): 이메일·`[매체 기자]`·`[지역=매체]`·"…기자 =" 정형구 제외 → 기자명(박성일·이병 등) 가짜 트렌드 제거.
- **노이즈 키워드 보강**(config, 총 63단어): 지면어(사설·칼럼…) + 필러(사람·자신·이야기…) + **정치일반어(의원·대표·대통령·국민·서울…)** + 지역(부산·경기…).
  - 정치일반어 제거가 핵심: 너무 지배적이라 실제 사건어(특검·김용태)를 가렸음.

### (5) VA 형용사 제외
- "좋다·경쾌하다"류(다-종결)가 라벨 오염 → sense 키워드에서 제외(VV 동사와 구분).

### (6) 동사(VV) 추출 + 설명가능한 라벨  ← 대표님 방향
- 검토 결과: "선고·환영·출시"(한자어 액션)는 NNG로 이미 잡힘. **순수동사(열리다·밝히다)는 VV로 누락**.
- **VV 추출 + 액션 태깅**(VV, 또는 NNG+하 동작명사) + 경동사 제외(하다·되다·나오다·지나다…).
- **라벨 = [엔티티명사] + [액션]** 조합: "이스라엘 공습", "정책 지원", "축제 열리다".
- 스레드 궤적이 사건 내러티브로: "후보 대선→대선 선거→이재명 선출", "이스라엘 공습→이란 공격→이란 공습".
- **트레이드오프**: 동사를 키워드에 더해 명사기반 anchor가 희석 → overall 0.744→0.685. 단 **군집 자체는 동일**(임베딩 불변), 라벨만 풍부. 목표 0.65는 충족. describability 우선(대표님 명시 요청)으로 유지.

### (7) 대표문장(rep) 수정
- 기존 rep = **최고 가중(제목)** → 군집과 무관한 제목 이상치 선정(예: 대선 군집 rep이 "이마트24 신임 대표 내정").
- **수정**: rep = **centroid 최근접 문장**(가장 대표적). 캐시 벡터 활용, 재추출 불필요, 점수 불변.

---

## 4. 인프라 / 빠른 반복

- **임베딩 캐시 하네스**: 임베딩(4분/주)이 비싸 재군집이 30분. `SQUADONE_SENSE_CACHE_DIR`로 1회 덤프(`data/sense_cache/*.pkl`) → 오프라인에서 K/linker/노이즈를 **초 단위** 스윕.
  - `scripts/sweep_k.py` — K + linker 스윕
  - `scripts/sweep_noise.py` — 노이즈 키워드(필러/동사) 스윕, `--write`로 DB 적재
  - `scripts/tune_linker.py` — linker 파라미터 그리드
  - `scripts/eval_trends.py` — DB 적재본 평가
- **원칙**: 키워드 단위 변경(노이즈·VA)은 캐시 후처리로 즉시 검증 가능(임베딩 불변). **문장 단위 변경(바이라인·동사)만 재추출(30분) 필요.**

---

## 5. 버그 수정 (이번 작업 중)

| 버그 | 내용 | 수정 |
|---|---|---|
| NNP 인물 미적용 | NER None이라 인물 누락 | NNP 태그 판별 |
| link_threads 성능 | 전 thread 전수 cos비교(10분+) | 키워드 역색인(바이트 동일 검증) |
| meta 누적 | trend_threads_meta가 thread_id로만 삭제→누적(4672행) | 범위 기준 선삭제 |
| raw 기본주차 | week 미지정시 최신주차 필터→빈 결과 | 프론트서 넓은 범위 조회 |
| action_words NameError | 호출 체인 전달 누락 | 함수 인자 전달 |

---

## 6. 프론트엔드 — 트렌드 대시보드

- `frontend/src/pages/TrendDashboard.tsx` (신규), 라우트 `/trend-dashboard`.
- **좌**: 주차 트렌드 랭킹(가중치 바·키워드). **우**: 스레드 타임라인(반복/진화, 점=가중치).
- **하단 상세 패널**: 트렌드 클릭 시 **사건 전개(라벨궤적 흐름) + 주차별 대표문장 + 키워드**.
- 데이터는 base 테이블 3개(`weekly_trend_clusters`/`trend_threads`/`trend_threads_meta`)를 raw API로 받아 클라이언트 조인.
- **네트워크 접속**: REST `0.0.0.0:8010`(--reload, DASHBOARD_ORIGINS), vite `--host 5180`(VITE_API_BASE=tailscale IP). → `http://100.106.14.57:5180/trend-dashboard`

---

## 7. 최종 설정값 (config/pipeline_config.json)

```jsonc
"keyword_extractor.sense": {
  "cluster_target_size": 135, "cluster_kmin": 10, "cluster_kmax": 110,
  "noise_keywords": [ ...63단어(지면어+필러+정치일반+지역)... ]
},
"trend_linker": {
  "alpha": 0.15, "beta": 0.85, "theta_near": 0.42, "theta_gap": 0.47,
  "max_gap_weeks": 30, "min_ent_jaccard": 0.25, "min_ent_jaccard_near": 0.10
}
```
- 코드 레벨: VV동사 추출 + `_GENERIC_VERBS` 제외, VA 제외, [엔티티]+[액션] 라벨, rep=centroid 최근접.

---

## 8. 검증 / 일반화

- **eval_trends**로 DB 적재본 = 인메모리 스윕 점수 일치 확인(0.6848).
- **2024 holdout**: 사건 무관 지표(coherence 0.501→0.511↑, label_div 유지)는 연도 넘어 일반화. overall 하락은 2024에 내란 사건이 없어 anchor(내란 전용)가 흩어진 메트릭 artifact — **과적합 아님**.
- "김문수 후보" rep 오류는 **과적합 아니라 rep 선정 로직 문제**로 확인·수정(군집 자체는 응집적: 873문장 중 김문수 291 vs 이마트 8).

---

## 9. 변경 파일 (전부 미커밋)

```
steps/keyword_extractor.py   동사추출·액션태깅·바이라인필터·VA제외·NNP
steps/keyword_sense.py        [엔티티]+[액션] 라벨·centroid rep·캐시덤프
steps/trend_linker.py         역색인 최적화
db/repository.py              meta 누적 버그 수정
config/pipeline_config.json   K·linker·noise 최종값
frontend/src/App.tsx          라우트·네비
frontend/src/pages/TrendDashboard.tsx   (신규) 대시보드
scripts/sweep_k.py            (신규) K 스윕
scripts/sweep_noise.py        (신규) 노이즈 스윕
```

> DB: `newstrend.weekly_trend_clusters`(2025·2024-W22~W26) + `trend_threads`/`trend_threads_meta`. Qdrant는 읽기전용.
