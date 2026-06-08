"""키워드 노이즈 분류 레이어.

z>=2.0 트렌드 후보에 섞이는 노이즈를 유형별로 라벨링한다(제거가 아니라 라벨/플래그).
1차 구현: seasonal(연도간 동일 ISO주차 재현). 후속: person/politics/buzzword.

설계·근거: memory/project_keyword_noise_classifier.md
산출: newstrend.keyword_class (class='seasonal', method='yoy_recurrence').
"""
from __future__ import annotations

import re
from collections import defaultdict
from typing import Dict

import pandas as pd

from steps.common import get_logger, load_config, log_step
from db import repository as repo

_WEEK_RE = re.compile(r"(\d{4})-W(\d{2})")


def _candidate_vocab(z_threshold: float) -> list[str]:
    """z>=threshold 스파이크를 가진 고유 키워드(분류 대상 어휘)."""
    spikes = repo.read_zscore_spikes(threshold=z_threshold)
    if spikes.empty:
        return []
    return sorted(spikes["keyword"].astype(str).unique().tolist())


def _parse_week(week: str) -> tuple[int, int] | None:
    m = _WEEK_RE.match(str(week))
    if not m:
        return None
    return int(m.group(1)), int(m.group(2))


def _seasonal_signal(
    spikes_by_kw: Dict[str, set[tuple[int, int]]],
    *,
    week_tolerance: int,
) -> list[dict]:
    """키워드별 (year, week) 스파이크 집합에서 연도간 동일주차 재현 신호를 계산.

    seas_yrs = 가장 강한 주차 클러스터([w-tol, w+tol])에서 스파이크한 '서로 다른 연도 수'.
    ratio    = seas_yrs / 관측연도수.
    """
    out: list[dict] = []
    for kw, yset in spikes_by_kw.items():
        if not yset:
            continue
        years_observed = len({y for (y, _w) in yset})
        wknums = {w for (_y, w) in yset}
        max_seasonal = 0
        best_wk = None
        for w in wknums:
            yrs = {y for (y, ww) in yset if abs(ww - w) <= week_tolerance}
            if len(yrs) > max_seasonal:
                max_seasonal = len(yrs)
                best_wk = w
        ratio = (max_seasonal / years_observed) if years_observed else 0.0
        out.append({
            "keyword": kw,
            "seas_yrs": int(max_seasonal),
            "years_observed": int(years_observed),
            "ratio": float(ratio),
            "best_wk": int(best_wk) if best_wk is not None else None,
            "distinct_weeks": int(len(wknums)),
            "n_spikes": int(len(yset)),
        })
    return out


def run_seasonal_classification() -> Dict[str, object]:
    """계절성 분류기: z>=threshold 스파이크의 연도간 동일주차 재현으로 seasonal 라벨링.

    정책: 라벨만 부여(자동 제거 아님). 임계는 config.keyword_class.seasonal.
    """
    logger = get_logger("steps.keyword_classifier")
    with log_step(logger, 0, "seasonal_classification", input="newstrend.z_score_keywords"):
        cfg = load_config()
        kc_cfg = cfg.get("keyword_class", {})
        s_cfg = kc_cfg.get("seasonal", {})
        z_threshold = float(kc_cfg.get("z_threshold", 2.0))
        week_tol = int(s_cfg.get("week_tolerance", 1))
        min_years = int(s_cfg.get("min_seasonal_years", 3))
        min_ratio = float(s_cfg.get("min_ratio", 0.6))
        # 집중도 보조조건: 1년 중 소수 주차에 집중해야 진짜 계절어(트루스소셜류 산재 오탐 제거).
        max_distinct_weeks = int(s_cfg.get("max_distinct_weeks", 15))
        dictionary = [str(w) for w in s_cfg.get("dictionary", []) if str(w).strip()]

        spikes = repo.read_zscore_spikes(threshold=z_threshold)
        if spikes.empty:
            raise ValueError(
                f"z>={z_threshold} 스파이크가 없습니다. (4단계 z_score_filtering 을 먼저 실행하세요)"
            )

        spikes_by_kw: Dict[str, set[tuple[int, int]]] = defaultdict(set)
        for kw, wk in spikes.itertuples(index=False, name=None):
            p = _parse_week(wk)
            if p:
                spikes_by_kw[str(kw)].add(p)
        vocab_set = set(spikes_by_kw.keys())

        signals = _seasonal_signal(spikes_by_kw, week_tolerance=week_tol)
        sig_df = pd.DataFrame(signals)
        n_candidates = len(sig_df)

        seasonal = sig_df[
            (sig_df["seas_yrs"] >= min_years)
            & (sig_df["ratio"] >= min_ratio)
            & (sig_df["distinct_weeks"] <= max_distinct_weeks)
        ].copy()
        logger.info(
            "seasonal 분류 | 후보키워드=%d | 라벨=%d | 기준: seas_yrs>=%d & ratio>=%.2f & distinct_weeks<=%d (week_tol=%d)",
            n_candidates, len(seasonal), min_years, min_ratio, max_distinct_weeks, week_tol,
        )

        def _write_dictionary() -> int:
            """ⓔ 띠 사전: 후보 어휘에 존재하는 사전 단어를 seasonal(method='dictionary')로 적재.
            12간지처럼 12년 주기라 연간 재현(yoy) 신호로는 약하게만 잡히는 계절어 보완."""
            present = [w for w in dictionary if w in vocab_set]
            dic_df = pd.DataFrame(
                [{"keyword": w, "score": 1.0, "detail": {"source": "dictionary"}} for w in present],
                columns=["keyword", "score", "detail"],
            )
            n = repo.write_keyword_class(dic_df, klass="seasonal", method="dictionary")
            logger.info("seasonal 사전 적재 | 사전=%d | 후보존재=%d", len(dictionary), n)
            return n

        if seasonal.empty:
            logger.warning("seasonal yoy 라벨 결과가 0건입니다. 임계값을 확인하세요.")
            n_db = repo.write_keyword_class(
                pd.DataFrame(columns=["keyword", "score", "detail"]),
                klass="seasonal", method="yoy_recurrence",
            )
            n_dic = _write_dictionary()
            return {"candidates": n_candidates, "labeled": n_dic, "db_rows": n_db + n_dic}

        # score = ratio(0~1). detail = 근거 수치.
        seasonal["score"] = seasonal["ratio"]
        seasonal["detail"] = seasonal.apply(
            lambda r: {
                "seas_yrs": int(r["seas_yrs"]),
                "years_observed": int(r["years_observed"]),
                "ratio": round(float(r["ratio"]), 4),
                "best_wk": (int(r["best_wk"]) if pd.notna(r["best_wk"]) else None),
                "distinct_weeks": int(r["distinct_weeks"]),
                "n_spikes": int(r["n_spikes"]),
            },
            axis=1,
        )
        n_db = repo.write_keyword_class(
            seasonal[["keyword", "score", "detail"]],
            klass="seasonal", method="yoy_recurrence",
        )
        logger.info("DB 적재 keyword_class(seasonal) | %d행", n_db)

        # 상위 미리보기 로그(검수용)
        preview = seasonal.sort_values(["seas_yrs", "ratio"], ascending=False).head(15)
        for _, r in preview.iterrows():
            logger.info(
                "  seasonal | %s | seas_yrs=%d | ratio=%.2f | best_wk=%s | distinct_weeks=%d",
                r["keyword"], int(r["seas_yrs"]), float(r["ratio"]),
                str(r["best_wk"]), int(r["distinct_weeks"]),
            )

        n_dic = _write_dictionary()
        return {
            "candidates": n_candidates,
            "labeled": int(len(seasonal)) + n_dic,
            "yoy_rows": n_db,
            "dictionary_rows": n_dic,
        }


def run_semantic_classification() -> Dict[str, object]:
    """의미기반 분류기: 후보 어휘를 카테고리 seed centroid와 비교(contrastive 최근접).

    각 키워드를 {politics, product, general, buzzword...} centroid와 코사인 비교해
    최근접 카테고리에 배정하되, target_categories 에 속하고 margin(1위-2위)이 임계 이상일 때만 라벨.
    (단일 centroid 절대 코사인은 anisotropy로 과대평가되므로 contrastive + margin 사용.)
    """
    logger = get_logger("steps.keyword_classifier")
    with log_step(logger, 0, "semantic_classification", input="newstrend.z_score_keywords"):
        import numpy as np
        from steps.qdrant_embed import embed_texts

        cfg = load_config()
        kc_cfg = cfg.get("keyword_class", {})
        z_threshold = float(kc_cfg.get("z_threshold", 2.0))
        sem = kc_cfg.get("semantic", {})
        targets = list(sem.get("target_categories", ["politics"]))
        margin_thr = float(sem.get("margin_threshold", 0.03))
        min_top_sim = float(sem.get("min_top_similarity", 0.5))
        seeds: dict[str, list[str]] = sem.get("seeds", {})
        dictionary: dict[str, list[str]] = sem.get("dictionary", {})
        if not seeds:
            raise ValueError("keyword_class.semantic.seeds 가 비어 있습니다.")

        vocab = _candidate_vocab(z_threshold)
        if not vocab:
            raise ValueError(f"z>={z_threshold} 스파이크 어휘가 없습니다. (4단계를 먼저 실행)")

        # 카테고리 centroid(seed 평균, L2 정규화)
        cat_names = list(seeds.keys())
        centroids = []
        for cat in cat_names:
            sv = embed_texts(list(seeds[cat]), normalize=True)
            c = sv.mean(axis=0)
            c = c / (np.linalg.norm(c) or 1.0)
            centroids.append(c)
        M = np.vstack(centroids)  # (n_cat, dim)

        V = embed_texts(vocab, normalize=True)  # (n_vocab, dim)
        sims = V @ M.T  # (n_vocab, n_cat)
        order = np.argsort(-sims, axis=1)
        top_idx = order[:, 0]
        top_sim = sims[np.arange(len(vocab)), top_idx]
        second_sim = sims[np.arange(len(vocab)), order[:, 1]]
        margin = top_sim - second_sim

        per_cat: dict[str, list[dict]] = {c: [] for c in targets}
        for i, kw in enumerate(vocab):
            cat = cat_names[top_idx[i]]
            if cat in per_cat and margin[i] >= margin_thr and top_sim[i] >= min_top_sim:
                per_cat[cat].append({
                    "keyword": kw,
                    "score": float(margin[i]),
                    "detail": {
                        "category": cat,
                        "top_sim": round(float(top_sim[i]), 4),
                        "margin": round(float(margin[i]), 4),
                        "runner_up": cat_names[order[i, 1]],
                    },
                })

        vocab_set = set(vocab)
        results: dict[str, int] = {}
        dict_results: dict[str, int] = {}
        total = 0
        for cat in targets:
            rows = per_cat.get(cat, [])
            df = pd.DataFrame(rows, columns=["keyword", "score", "detail"])
            n_db = repo.write_keyword_class(df, klass=cat, method="semantic_centroid")
            results[cat] = n_db
            total += n_db
            logger.info(
                "semantic 분류 | category=%s | 라벨=%d | margin>=%.2f & top_sim>=%.2f",
                cat, n_db, margin_thr, min_top_sim,
            )
            for r in sorted(rows, key=lambda x: x["score"], reverse=True)[:12]:
                logger.info("  %s | %s | margin=%.3f | top_sim=%.3f | 2위=%s",
                            cat, r["keyword"], r["detail"]["margin"],
                            r["detail"]["top_sim"], r["detail"]["runner_up"])

            # 사전 보완: contrastive로 불안정한 기지 약어를 확정 라벨(method='dictionary')
            dict_words = [w for w in dictionary.get(cat, []) if w in vocab_set]
            dic_df = pd.DataFrame(
                [{"keyword": w, "score": 1.0, "detail": {"source": "dictionary"}} for w in dict_words],
                columns=["keyword", "score", "detail"],
            )
            n_dic = repo.write_keyword_class(dic_df, klass=cat, method="dictionary")
            dict_results[cat] = n_dic
            total += n_dic
            logger.info("semantic 사전 적재 | category=%s | 사전=%d | 후보존재=%d",
                        cat, len(dictionary.get(cat, [])), n_dic)

        return {"vocab": len(vocab), "labeled_total": total,
                "by_category": results, "dictionary": dict_results}


_HANGUL_RE = re.compile(r"^[가-힣]+$")

# 무공백 부착이 흔한 경칭 — '홍길동씨'처럼 키워드에 붙어 쓰여 공백 경계 검사를 면제한다.
_HONORIFIC_TITLES = frozenset({"씨", "군", "양", "옹"})


def _is_namelike(word: str, surnames: set[str], name_len: tuple[int, int]) -> bool:
    """이름꼴 사전필터: 한글만 + 길이 name_len + 흔한 성씨로 시작.

    '외국 기자'·'담당 판사'처럼 직책어 앞에 오지만 이름 아닌 NNG는 성씨 조건에서 배제된다.
    품사(NNP) 검증은 단독 태깅이 불안정(심준보→[심/주/ㄴ/보], 강태윤→NNG)하므로
    여기서 하지 않고, 문맥이 있는 _title_adjacent_hits 에서 in-context 로 적용한다.
    """
    return bool(_HANGUL_RE.match(word)) and (name_len[0] <= len(word) <= name_len[1]) and word[0] in surnames


def _title_adjacent_hits(keyword: str, texts: list[str], kiwi, titles: set[str]) -> tuple[int, list[str]]:
    """texts(기사 스니펫) 중 keyword 토큰 '바로 다음'이 직책어인 기사 수(기사당 최대 1) + 직책어 목록.

    정밀도 보강 2종(일반명사 오탐 차단):
      ① 인명 증거(ever-NNP): 스니펫 중 '한 번이라도' keyword 가 고유명사(NNP)로
         인식돼야 인정. 일반명사 '권투/도핑/도매상/강골/고별' 은 모든 문맥에서 NNG 라 탈락.
         (occurrence 별 NNP 요구는 바이라인 'OOO 기자' 문맥에서 진짜 인명도 NNG 가 돼
          과탈락하므로 — 심준보/맹성규/류희림 — '한 번이라도 NNP' 게이트로 완화.)
      ② 공백 경계: 동형이의 일반명사 직책어('선수/검사/대표/기자' 등)는 '권투선수'·'도핑검사'
         처럼 복합어를 Kiwi 가 쪼갠 인접이 오탐을 만든다. keyword 와 직책어 사이 공백 경계가
         있을 때만 카운트(경칭 씨/군/양/옹 은 무공백 부착이 흔해 면제).
    ever-NNP 미충족 시 hits=0(라벨 불가). 우연히 NNP 로 잡히는 잔존 오탐(기감/도핑 등)은
    config person.exclude_words 로 후보 단계에서 차단한다.
    """
    ever_nnp = False
    found: list[str] = []
    for t in texts:
        toks = kiwi.tokenize(t)
        article_hit: str | None = None
        for i in range(len(toks) - 1):
            cur, nxt = toks[i], toks[i + 1]
            if cur.form != keyword:
                continue
            if cur.tag.startswith("NNP"):
                ever_nnp = True  # 인명 증거(전체 스니펫 누적)
            if article_hit is None and nxt.form in titles:
                # 비경칭 직책어는 공백 경계가 있을 때만(복합어 분해 오탐 차단)
                if nxt.form in _HONORIFIC_TITLES or nxt.start - (cur.start + cur.len) > 0:
                    article_hit = nxt.form
        if article_hit is not None:
            found.append(article_hit)
    if not ever_nnp:
        return 0, []
    return len(found), found


def _scroll_body_texts(base: str, collection: str, field: str, keyword: str, limit: int, timeout_sec: float) -> list[str]:
    """Qdrant scroll + payload 전문매치(match.text)로 keyword를 '실제 포함'하는 기사의 title+body를 반환.

    벡터검색(search_contexts)은 의미 유사 기사를 주므로 희귀 인명이 본문에 없을 수 있다.
    직책어 인접 판정엔 키워드가 실제 등장한 기사가 필요하므로 전문매치 스크롤을 쓴다.
    """
    import json
    from urllib import request

    payload = json.dumps({
        "limit": int(limit),
        "with_payload": True,
        "filter": {"must": [{"key": field, "match": {"text": keyword}}]},
    }).encode("utf-8")
    req = request.Request(
        f"{base}/collections/{collection}/points/scroll",
        data=payload, headers={"Content-Type": "application/json"}, method="POST",
    )
    with request.urlopen(req, timeout=timeout_sec) as resp:
        pts = json.loads(resp.read().decode("utf-8")).get("result", {}).get("points", []) or []
    out: list[str] = []
    for p in pts:
        pl = p.get("payload") or {}
        out.append(" ".join([
            str(pl.get("title") or pl.get("제목") or ""),
            str(pl.get("body") or pl.get("본문") or pl.get("content") or ""),
        ]))
    return out


def run_person_classification() -> Dict[str, object]:
    """인명 분류기 — 직책어 문맥 규칙(NER 미사용).

    실증: Kiwi NER(PS)은 맨단어/인-맥락 모두 None이라 못 쓴다. 대신 in-context 품사가
    유효 — 인명은 문맥에서 NNP(심준보·강태윤·권대희), 일반어는 NNG(권투·도핑·도매상)로
    갈린다. 견고한 신호 = '인명(NNP)은 직책어(의원·판사·기자·씨…) 앞에 온다'. 그래서:
      ① 이름꼴 후보(성씨+길이2~3)만 추리고,
      ② 각 후보의 Qdrant 스니펫에서 키워드가 NNP로 인식된 직책어 인접 빈도를 세어,
         (복합어 분해 오탐은 공백 경계 검사로 차단 — _title_adjacent_hits 참조)
      ③ min_title_hits 이상이면 person 라벨(method='context_title').
    Qdrant 컨텍스트가 필요하므로 search_contexts(대표 스파이크 주차) 사용.
    kiwipiepy/Qdrant 미가용 시 안전하게 건너뛴다.
    """
    logger = get_logger("steps.keyword_classifier")
    with log_step(logger, 0, "person_classification", input="newstrend.z_score_keywords + Qdrant"):
        try:
            from kiwipiepy import Kiwi
        except ImportError:
            logger.warning("kiwipiepy 미설치 — person 분류 건너뜀(Kiwi 환경에서 실행 필요).")
            return {"skipped": "kiwipiepy_not_installed", "labeled": 0}

        cfg = load_config()
        kc_cfg = cfg.get("keyword_class", {})
        z_threshold = float(kc_cfg.get("z_threshold", 2.0))
        p_cfg = kc_cfg.get("person", {})
        if not bool(p_cfg.get("enabled", True)):
            logger.info("person 분류 비활성(config) — 건너뜀.")
            return {"disabled": True, "labeled": 0}

        model_type = str(p_cfg.get("model_type", "cong"))
        surnames = set(p_cfg.get("surnames", []))
        titles = set(p_cfg.get("titles", []))
        _nl = p_cfg.get("name_len", [2, 3])
        name_len = (int(_nl[0]), int(_nl[1]))
        min_title_hits = int(p_cfg.get("min_title_hits", 3))
        top_k_snippets = int(p_cfg.get("top_k_snippets", 10))
        if not surnames or not titles:
            raise ValueError("keyword_class.person.surnames/titles 가 비어 있습니다.")

        match_field = str(p_cfg.get("match_field", "body"))
        from db.config import qdrant_url as _qurl
        vcfg = cfg.get("vector_db", {})
        base = str(vcfg.get("qdrant_url") or _qurl()).rstrip("/")
        collection = str(vcfg.get("collection", "news_10y_ko_v1"))
        timeout_sec = float(vcfg.get("timeout_sec", 15))

        # 이름꼴 후보(성씨+길이): z>=2.0 어휘에서 추림. 정밀 판정은 직책어 인접에서.
        # exclude_suffixes: 학과·부처·기관 등 비인명 접미(부/과/처/학/원…) 제거. 정밀도 우선
        # (over-block은 status quo와 동일·무해, FP는 실트렌드 오제외라 유해).
        exclude_suffixes = tuple(p_cfg.get("exclude_suffixes", []))
        # exclude_words: ever-NNP 게이트를 우연히 통과하는 잔존 오탐(일반명사·회사명) 수동 차단.
        exclude_words = set(p_cfg.get("exclude_words", []))
        vocab = _candidate_vocab(z_threshold)
        candidates = [
            kw for kw in vocab
            if _is_namelike(kw, surnames, name_len)
            and kw not in exclude_words
            and not (exclude_suffixes and kw.endswith(exclude_suffixes))
        ]
        logger.info(
            "person 후보 | z>=%.1f 어휘=%d | 이름꼴 후보=%d | min_title_hits=%d | scroll_limit=%d | field=%s",
            z_threshold, len(vocab), len(candidates), min_title_hits, top_k_snippets, match_field,
        )
        if not candidates:
            repo.write_keyword_class(pd.DataFrame(columns=["keyword", "score", "detail"]),
                                     klass="person", method="context_title")
            return {"vocab": len(vocab), "candidates": 0, "labeled": 0}

        # 환경별 모델 가용성 차이: model_type 미지정/'default'이면 kiwipiepy 기본 모델 사용.
        # (지정 모델 미설치 시 Kiwi가 C++ std::terminate로 죽어 try/except로 못 잡으므로 빈 값 권장.)
        kiwi_kwargs: dict = {"load_default_dict": True}
        if model_type and model_type.lower() not in ("", "default"):
            kiwi_kwargs["model_type"] = model_type
        kiwi = Kiwi(**kiwi_kwargs)

        rows: list[dict] = []
        n_err = 0
        for idx, kw in enumerate(candidates, start=1):
            try:
                texts = _scroll_body_texts(base, collection, match_field, kw, top_k_snippets, timeout_sec)
            except Exception as exc:
                n_err += 1
                if n_err <= 5:
                    logger.warning("person 본문매치 실패 | kw=%s | %s", kw, exc)
                continue
            hits, found = _title_adjacent_hits(kw, texts, kiwi, titles)
            if hits >= min_title_hits:
                rows.append({
                    "keyword": kw, "score": float(hits),
                    "detail": {"hits": hits, "titles": sorted(set(found))[:8], "articles": len(texts)},
                })
            if idx % 500 == 0:
                logger.info("person 진행 | %d/%d | 누적라벨=%d | 검색오류=%d", idx, len(candidates), len(rows), n_err)

        df = pd.DataFrame(rows, columns=["keyword", "score", "detail"])
        n_db = repo.write_keyword_class(df, klass="person", method="context_title")
        logger.info("person(직책어문맥) 분류 | 후보=%d | 라벨=%d | model=%s", len(candidates), n_db, model_type)
        for r in sorted(rows, key=lambda x: x["score"], reverse=True)[:15]:
            logger.info("  person | %s | hits=%d | titles=%s", r["keyword"], r["detail"]["hits"], r["detail"]["titles"])
        return {"vocab": len(vocab), "candidates": len(candidates), "labeled": n_db}


def run_keyword_classification(which: str = "all") -> Dict[str, object]:
    """분류기 오케스트레이터. which: 'seasonal'|'semantic'|'person'|'all'."""
    out: Dict[str, object] = {}
    if which in ("seasonal", "all"):
        out["seasonal"] = run_seasonal_classification()
    if which in ("semantic", "all"):
        out["semantic"] = run_semantic_classification()
    if which in ("person", "all"):
        out["person"] = run_person_classification()
    return out


if __name__ == "__main__":
    from steps.common import configure_logging

    configure_logging("INFO", None, stream="stdout")
    print(run_keyword_classification("all"))
