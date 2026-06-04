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


def run_person_classification() -> Dict[str, object]:
    """고유명사(person 후보) 분류기: 후보 어휘를 Kiwi로 단독 토큰화해 고유명사를 라벨.

    한계(검증으로 확인됨): 맨단어 토큰화에서는 Kiwi NER(PS)이 동작하지 않고(전부 None),
    1단계에서 누락된 인명(예: 김관태·심준보)은 NNG(일반명사)로 태깅돼 일반어와 구분 불가하다.
    오직 이미 사전에 등재된 유명 고유명사만 NNP로 잡힌다(윤석열·손흥민). 따라서 본 분류기는
    '인명 정밀 탐지'가 아니라 '고유명사(person·기관·지명·브랜드 혼재) 약신호'다(score=0.5).
    → 5단계 anchor 제외 기본 대상에서 제외하고 review 용도로만 사용하는 것을 권장.
    근본적 인명 제거는 문맥 NER(1단계 재토큰화)이 필요(별도 과제).

    kiwipiepy 미설치 시 안전하게 건너뛴다.
    """
    logger = get_logger("steps.keyword_classifier")
    with log_step(logger, 0, "person_classification", input="newstrend.z_score_keywords"):
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
        # sbg 모델은 kiwipiepy 0.23에서 맨단어 루프 시 segfault → 안정적인 'cong' 기본 사용.
        model_type = str(p_cfg.get("model_type", "cong"))

        vocab = _candidate_vocab(z_threshold)
        if not vocab:
            raise ValueError(f"z>={z_threshold} 스파이크 어휘가 없습니다. (4단계를 먼저 실행)")

        kiwi = Kiwi(model_type=model_type, load_default_dict=True)

        def _classify(word: str) -> tuple[bool, str, float]:
            toks = kiwi.tokenize(word)
            # 강신호: NER PS(현 버전 맨단어에선 미동작이나 향후 호환 위해 유지)
            for t in toks:
                ner = getattr(t, "ner", None)
                if ner:
                    first = ner[0] if isinstance(ner, (list, tuple)) and ner else ner
                    label = str(first[0]) if isinstance(first, (list, tuple)) and first else str(first)
                    if label == "PS":
                        return True, "ner_PS", 1.0
            # 약신호: 단일 고유명사(NNP) — person/기관/지명/브랜드 혼재
            if len(toks) == 1 and str(toks[0].tag) == "NNP":
                return True, "single_NNP", 0.5
            return False, "", 0.0

        rows: list[dict] = []
        for kw in vocab:
            ok, why, score = _classify(kw)
            if ok:
                rows.append({"keyword": kw, "score": score, "detail": {"reason": why}})
        df = pd.DataFrame(rows, columns=["keyword", "score", "detail"])
        n_db = repo.write_keyword_class(df, klass="person", method="kiwi_nnp")
        n_ps = sum(1 for r in rows if r["detail"]["reason"] == "ner_PS")
        logger.info("person(고유명사) 분류 | 어휘=%d | 라벨=%d (PS강신호=%d, NNP약신호=%d) | model=%s",
                    len(vocab), n_db, n_ps, n_db - n_ps, model_type)
        for r in rows[:15]:
            logger.info("  person | %s | %s | score=%.1f", r["keyword"], r["detail"]["reason"], r["score"])
        return {"vocab": len(vocab), "labeled": n_db, "ps_strong": n_ps, "nnp_weak": n_db - n_ps}


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
