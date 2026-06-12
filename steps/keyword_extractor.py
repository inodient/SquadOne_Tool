from __future__ import annotations

import bisect
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import pandas as pd

from steps.common import (
    ensure_output_dir,
    get_logger,
    load_config,
    log_artifact,
    log_step,
    resolve_path,
    write_csv,
    write_dataframe_json_export,
)
from steps.qdrant_news import iter_week_articles, weeks_in_range

try:
    from kiwipiepy import Kiwi
except ImportError:  # pragma: no cover - optional dependency
    Kiwi = None


def _load_stopwords(path: Path) -> set[str]:
    if not path.exists():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def _pick_column(columns: List[str], candidates: List[str]) -> str | None:
    lowered = {c.lower(): c for c in columns}
    for candidate in candidates:
        if candidate.lower() in lowered:
            return lowered[candidate.lower()]
    return None


def _iso_week_label(date_series: pd.Series) -> pd.Series:
    dt = pd.to_datetime(date_series, errors="coerce")
    iso = dt.dt.isocalendar()
    return iso["year"].astype(str) + "-W" + iso["week"].astype(str).str.zfill(2)


def _coerce_news_date_series(raw: pd.Series) -> pd.Series:
    """
    뉴스 데이터의 `일자`는 종종 YYYYMMDD 형태의 정수(int/float)로 들어온다.
    이 상태에서 pd.to_datetime을 포맷 없이 호출하면 epoch ns로 오인되어 1970년대 날짜가 된다.
    """
    s = raw
    # 숫자형(정수/실수) 우선: 8자리 YYYYMMDD로 해석
    if pd.api.types.is_numeric_dtype(s):
        s_int = pd.to_numeric(s, errors="coerce").round(0).astype("Int64")
        dt = pd.to_datetime(s_int.astype(str), format="%Y%m%d", errors="coerce")
        return dt

    # 문자열: 숫자 8자리면 YYYYMMDD로, 아니면 일반 파싱
    s_str = s.astype(str).str.strip()
    mask = s_str.str.fullmatch(r"\d{8}")
    out = pd.Series(pd.NaT, index=s.index, dtype="datetime64[ns]")
    if mask.any():
        out.loc[mask] = pd.to_datetime(s_str.loc[mask], format="%Y%m%d", errors="coerce")
    if (~mask).any():
        out.loc[~mask] = pd.to_datetime(s_str.loc[~mask], errors="coerce")
    return out


def _normalize_noun(word: str) -> str:
    """NNG/VA 토큰에서 기호·한글자모(완성형 외) 제거. 숫자/영문/완성형 한글만 남긴다.

    Kiwi sbg 가 일부 토큰을 기호 포함으로 NNG 오태깅한다:
      - 앞뒤 기호: '0.25%포인트'→'포인트', '"여죄를'→'여죄', '!더중플'→'더중플'
      - 내부 기호·깨진 자모: '시ᆞ군'→'시군', '의료ᆞ요양ᆞ돌봄'→'의료요양돌봄', '이접들#2'→'이접들2'
    숫자는 의미일 수 있어 보존('1심'·'2심'·'제3자'·'코로나19'·'2루수'는 살아남는다).
    정상 명사는 내부에 비단어문자가 없어 영향받지 않는다.
    """
    return re.sub(r"[^0-9A-Za-z가-힣]", "", word.strip())


def _is_valid_noun(word: str) -> bool:
    """채택 조건: 2자 이상 + (한글 1자 이상 OR 영문 2자 이상 연속).

    순수 숫자/기호/영문1자(50·-35·R1)는 배제하되, 제품·모델명(EV9·HBM3·SU7)과
    영문 약어(AI·IT)는 보존한다 — 상품군 추천에 모델명이 의미 있는 신호이기 때문.
    """
    return len(word) >= 2 and bool(re.search(r"[가-힣]|[A-Za-z]{2,}", word))


# 범용/경동사(트렌드 행위로 의미 없음) — VV 추출 시 제외. 액션성 동사(열리다·밝히다·나서다)는 보존.
_GENERIC_VERBS = {
    "하다", "되다", "있다", "없다", "같다", "말다", "보다", "오다", "가다", "주다", "받다",
    "싶다", "않다", "못하다", "위하다", "대하다", "통하다", "따르다", "들다", "나다", "두다",
    "내다", "지다", "삼다", "맞다", "남다", "쓰다", "놓다", "넣다", "보이다", "알다", "모르다",
    "나오다", "지나다", "만나다", "나타나다", "지내다", "들어가다", "나가다", "들어오다",
    "나서다", "보내다", "다니다", "지키다", "이루다", "이르다",
}


def _extract_nouns_with_pos(
    text: str, kiwi: "Kiwi | None", stopwords: set[str], *,
    include_person: bool = False, include_verbs: bool = False,
) -> List[Tuple[str, int, bool, str]]:
    """(공백 정규화 text 기준) char 시작위치 + is_person + action_type 부착 추출.

    맥락(context) 수집 시 명사 위치를 어절에 매핑하는 데 쓴다.
    include_person=True 면 고유명사(NNP)도 포함(sense/트렌드용). 기본 제외(weekly_keywords 불변).
    include_verbs=True 면 순수동사(VV)도 포함(설명가능한 말묶음용). 기본 제외(weekly_keywords 불변).
    action_type: ''(엔티티) | 'verb'(VV 동사) | 'noun'(NNG+하/되/시키=동작명사 선고하다).
    Kiwi(sbg) tokenize 는 NER 을 주지 않으므로 NNP 태그로 인물 판별한다.
    반환 (word, start, is_person, action_type).
    """
    text = re.sub(r"\s+", " ", str(text)).strip()
    if not text:
        return []
    if kiwi is not None:
        out: List[Tuple[str, int, bool, str]] = []
        toks = kiwi.tokenize(text)
        for idx, token in enumerate(toks):
            tag = token.tag
            if tag.startswith("J"):
                continue
            is_person = tag == "NNP"
            action_type = ""
            if is_person:
                if not include_person:
                    continue
                word = _normalize_noun(token.form)  # 고유명사(NNP)를 sense/트렌드용으로 포함
            elif tag == "NNG":
                word = _normalize_noun(token.form)
                nxt = toks[idx + 1] if idx + 1 < len(toks) else None
                if nxt is not None and nxt.tag == "XSV":
                    action_type = "noun"  # 동작명사(선고+하다 → '선고' 자체는 NNG, 행위 표지)
            elif tag == "VV":
                if not include_verbs:
                    continue
                base = _normalize_noun(token.form)
                word = "" if not base else (base if base.endswith("다") else f"{base}다")
                if word in _GENERIC_VERBS:
                    continue  # 경동사 제외
                action_type = "verb"
            elif tag == "VA":
                base = _normalize_noun(token.form)
                word = "" if not base else (base if base.endswith("다") else f"{base}다")
            else:
                continue
            if _is_valid_noun(word) and word not in stopwords:
                out.append((word, int(getattr(token, "start", 0)), is_person, action_type))
        return out
    return [
        (m.group(), m.start(), False, "")
        for m in re.finditer(r"[가-힣]{2,}", text)
        if m.group() not in stopwords
    ]


def _accumulate_context(
    norm_text: str,
    noun_pos: List[Tuple[str, int]],
    ctx_counter: Dict[str, Counter],
    nbr_counter: Dict[str, Dict[str, Counter]],
    *,
    before_n: int = 2,
    after_n: int = 2,
) -> None:
    """각 명사의 앞/뒤 어절(띄어쓰기 단위)을 수집.

    norm_text 는 _extract_nouns_with_pos 와 동일하게 공백 정규화된 본문이어야 위치가 맞다.
    명사 char 위치 → bisect 로 소속 어절 → 앞 before_n / 뒤 after_n 어절.
    """
    eojeols = [(m.group(), m.start()) for m in re.finditer(r"\S+", norm_text)]
    if not eojeols:
        return
    starts = [s for _, s in eojeols]
    n = len(eojeols)
    for word, pos in noun_pos:
        i = bisect.bisect_right(starts, pos) - 1
        if i < 0:
            continue
        before = [eojeols[j][0] for j in range(max(0, i - before_n), i)]
        after = [eojeols[j][0] for j in range(i + 1, min(n, i + 1 + after_n))]
        ctx_counter[word][(" ".join(before), " ".join(after))] += 1
        nb = nbr_counter[word]
        for t in before:
            nb["before"][t] += 1
        for t in after:
            nb["after"][t] += 1


def _sentence_spans(norm_text: str) -> List[Tuple[str, int]]:
    """공백 정규화된 본문을 문장으로 분리. (문장텍스트, 시작 char위치) 목록.

    위치는 norm_text(=_extract_nouns_with_pos 입력과 동일) 기준이라 명사 char위치와 정렬된다.
    종결부호(. ! ? 。！？) + 공백을 경계로 자른다(개행은 이미 공백으로 정규화됨).
    """
    spans: List[Tuple[str, int]] = []
    pos = 0
    for m in re.finditer(r"(?<=[\.\!\?。！？])\s+", norm_text):
        spans.append((norm_text[pos:m.start()].strip(), pos))
        pos = m.end()
    if pos < len(norm_text):
        spans.append((norm_text[pos:].strip(), pos))
    return spans


# 바이라인/보일러플레이트 문장 탐지 — 기자명·매체명이 NNP 인물로 유입돼 트렌드 라벨을
# 오염시키는 것을 차단(예: "[아시아투데이 이병화]", "한윤종 기자 hyj0709@segye.com",
# "아시아투데이 박성일 기자 = …", "기사 특정내용과 무관."). 고정밀(일반 문장 오탐 최소).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_BRACKET_BYLINE_RE = re.compile(r"^[\[\(][^\]\)]{0,40}[\]\)]$")
_REPORTER_PREFIX_RE = re.compile(r"^.{0,25}?(기자|특파원|논설위원|앵커)\s*[=:]")
_BYLINE_MARK_RE = re.compile(r"(기자|특파원|논설위원|뉴스|투데이|일보|신문|닷컴|통신)")
_BOILERPLATE_RE = re.compile(r"무단\s*전재|재배포\s*금지|저작권자|특정\s*내용과\s*무관|영상편집|그래픽\s*=")


def _is_byline_or_boilerplate(text: str) -> bool:
    """기자 바이라인/매체 정형구 문장이면 True(수집 제외). 일반 문장은 통과."""
    t = (text or "").strip()
    if not t:
        return True
    if _EMAIL_RE.search(t):
        return True  # 이메일 포함 = 거의 항상 바이라인
    if _BOILERPLATE_RE.search(t):
        return True  # 저작권·무관 등 정형구
    if _BRACKET_BYLINE_RE.match(t) and (_BYLINE_MARK_RE.search(t) or "=" in t):
        return True  # [매체명 기자명] 또는 [지역=매체] 형태
    if _REPORTER_PREFIX_RE.match(t):
        return True  # "…기자 = 본문" 의 바이라인 선두(기자명 오염 차단)
    return False


def _accumulate_sentences(
    title: str,
    norm_body: str,
    noun_pos: List[Tuple[str, int, str]],
    kiwi: "Kiwi | None",
    stopwords: set[str],
    sent_weight: Dict[str, float],
    sent_kws: Dict[str, Counter],
    action_words: set[str],
    *,
    title_weight: float,
    body_base: float,
    body_decay: float,
    min_sent_w: float,
    min_sent_len: int,
    lead_n: int,
    noise_kw: set[str] | None = None,
) -> None:
    """sense(대안 C) 입력 수집 — 문장 중심. 제목 + 본문 리드 lead_n 문장만.

    우선순위: 제목(title_weight) > 본문. 본문은 앞 문장일수록 큰 가중(body_base * body_decay**i).
    sent_weight[문장] += weight,  sent_kws[문장][키워드] += 1 (라벨·키워드 매핑용).
    noise_kw 의 상용어는 키워드에서 제외(라벨 노이즈·문장 수 절감); 남는 키워드 없으면 문장 자체 제외.
    한 문장은 (여러 기사에 반복돼도) 같은 키로 누적 → 전역 군집 시 1회만 임베딩된다.
    """
    noise = noise_kw or set()

    def _filter_va(words: set[str]) -> set[str]:
        # VA 형용사(다-종결 비액션)는 제외, VV 동사(다-종결 액션=action_words)는 보존.
        return {w for w in words if not (w.endswith("다") and w not in action_words)}

    # 제목 — 한 문장으로 취급, 높은 가중.
    t = re.sub(r"\s+", " ", str(title or "")).strip()
    if t and len(t) >= 2 and not _is_byline_or_boilerplate(t):
        t_toks = _extract_nouns_with_pos(t, kiwi, stopwords, include_person=True, include_verbs=True)
        for w, _p, _isp, at in t_toks:
            if at:
                action_words.add(w)  # 액션(동사/동작명사) 표지 수집 → 라벨 조합용
        t_nouns = _filter_va({w for w, _, _, _ in t_toks} - noise)
        if t_nouns:
            sent_weight[t] += title_weight
            kc = sent_kws[t]
            for w in t_nouns:
                kc[w] += 1

    # 본문 — 앞 lead_n 문장만(역피라미드: 핵심이 앞에). 그 뒤 명사는 버림.
    full = _sentence_spans(norm_body)
    if not full:
        return
    lead = full[:lead_n] if lead_n > 0 else full
    cutoff = full[lead_n][1] if (0 < lead_n < len(full)) else len(norm_body) + 1
    starts = [st for _, st in lead]
    sent_nouns: Dict[int, set] = defaultdict(set)
    for word, pos, atype in noun_pos:
        if atype:
            action_words.add(word)  # 액션 표지 수집(컷오프 무관, 전역 단어 속성)
        if pos >= cutoff:
            continue
        i = bisect.bisect_right(starts, pos) - 1
        if i < 0:
            continue
        sent_nouns[i].add(word)
    for i, nouns in sent_nouns.items():
        text = lead[i][0]
        if len(text) < min_sent_len:
            continue
        if _is_byline_or_boilerplate(text):
            continue  # 기자 바이라인/정형구 문장 제외(기자명 라벨 오염 차단)
        kept = _filter_va(nouns - noise)  # VA 형용사 제외(VV 동사는 보존)
        if not kept:
            continue  # 노이즈/형용사만 있는 문장(운세·포토 등) 제외
        wt = body_base * (body_decay ** i)
        if wt < min_sent_w:
            wt = min_sent_w
        sent_weight[text] += wt
        kc = sent_kws[text]
        for w in kept:
            kc[w] += 1


def _extract_nouns(text: str, kiwi: Kiwi | None, stopwords: set[str]) -> List[str]:
    text = re.sub(r"\s+", " ", str(text)).strip()
    if not text:
        return []

    if kiwi is not None:
        def _is_person_entity(token_obj: object) -> bool:
            ner = getattr(token_obj, "ner", None)
            if not ner:
                return False
            try:
                # 일반적으로 ("PS", score) 혹은 [("PS", score), ...] 형태
                if isinstance(ner, (list, tuple)):
                    first = ner[0]
                    if isinstance(first, (list, tuple)) and first:
                        return str(first[0]) == "PS"
                    return str(first) == "PS"
            except Exception:
                return False
            return False

        tokens: List[str] = []
        # 조사(J*)는 기본 제거
        for token in kiwi.tokenize(text):
            # 구버전/신버전 호환을 위해 조사(J*)는 후처리로 제외한다.
            if token.tag.startswith("J"):
                continue
            # 인명 개체(PS) 제외
            if _is_person_entity(token):
                continue

            # NNG: 일반명사 / VA: 형용사(기본형 처리). 앞뒤 기호 정제 후 채택.
            if token.tag == "NNG":
                word = _normalize_noun(token.form)
            elif token.tag == "VA":
                base = _normalize_noun(token.form)
                word = "" if not base else (base if base.endswith("다") else f"{base}다")
            else:
                continue

            if _is_valid_noun(word) and word not in stopwords:
                tokens.append(word)
        return tokens

    # Kiwi가 없는 경우 간단한 한글 명사 유사 토큰 추출
    # NOTE: 문자 클래스 `[가-힣]`는 모든 현대 한글 음절을 커버하지 못할 수 있어 유니코드 범위를 사용한다.
    rough_tokens = re.findall(r"[\uac00-\ud7a3]{2,}", text)
    return [tok for tok in rough_tokens if tok not in stopwords]


def _extract_feature_keywords(feature_text: str, stopwords: set[str]) -> List[str]:
    """
    `특성추출(가중치순 상위 50개)` 컬럼 파싱 전용.
    예: "반도체(0.51), 수출(0.32)" -> ["반도체", "수출"]
    """
    text = str(feature_text).strip()
    if not text or text.lower() == "nan":
        return []
    parts = re.split(r"[,\|;/\n]+", text)
    tokens: List[str] = []
    for part in parts:
        token = part.strip()
        if not token:
            continue
        token = re.sub(r"\([^)]*\)", "", token).strip()
        token = re.sub(r"\[[^\]]*\]", "", token).strip()
        token = re.sub(r"^\d+[\.\)]\s*", "", token).strip()
        token = re.sub(r"\s+", " ", token).strip()
        if len(token) >= 2 and token not in stopwords:
            tokens.append(token)
    return tokens


def _parse_iso_date(label: str, value: str | None) -> pd.Timestamp | None:
    if value is None:
        return None
    ts = pd.to_datetime(value, errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"{label} 날짜 형식이 올바르지 않습니다(YYYY-MM-DD 권장): {value}")
    return pd.Timestamp(ts).normalize()


def _parse_yyyymmdd(value: str) -> pd.Timestamp:
    ts = pd.to_datetime(value, format="%Y%m%d", errors="coerce")
    if pd.isna(ts):
        raise ValueError(f"YYYYMMDD 파싱 실패: {value}")
    return pd.Timestamp(ts).normalize()


def _parse_filename_range(path: Path, pattern: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    m = re.match(pattern, path.name)
    if not m:
        return None
    start = _parse_yyyymmdd(m.group(1))
    end = _parse_yyyymmdd(m.group(2))
    if start > end:
        # 파일명이 뒤바뀐 경우에도 최대한 처리 가능하게 swap
        start, end = end, start
    end_inclusive = end + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)
    return start, end_inclusive


def _ranges_overlap(
    a_start: pd.Timestamp | None,
    a_end_inclusive: pd.Timestamp | None,
    b_start: pd.Timestamp,
    b_end_inclusive: pd.Timestamp,
) -> bool:
    # a가 None이면 필터가 열려있다고 간주(겹침)
    if a_start is None and a_end_inclusive is None:
        return True
    if a_start is None:
        return b_start <= a_end_inclusive
    if a_end_inclusive is None:
        return b_end_inclusive >= a_start
    return not (b_end_inclusive < a_start or b_start > a_end_inclusive)


def _scan_filename_dataset_range(files: List[Path], pattern: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    starts: list[pd.Timestamp] = []
    ends: list[pd.Timestamp] = []
    for p in files:
        fr = _parse_filename_range(p, pattern)
        if fr is None:
            continue
        s, e = fr
        starts.append(s)
        ends.append(e)
    if not starts or not ends:
        return None
    return min(starts), max(ends)


def _resolve_filename_filter_enabled(raw: object, *, has_user_range: bool) -> bool:
    """
    - true/false: 강제 on/off
    - "auto"(기본): 사용자 기간(start/end 중 하나라도 지정)이 있을 때만 파일명 필터 ON
    """
    if raw is None:
        raw = "auto"
    if isinstance(raw, str):
        v = raw.strip().lower()
        if v in {"auto", ""}:
            return bool(has_user_range)
        if v in {"1", "true", "yes", "y", "on"}:
            return True
        if v in {"0", "false", "no", "n", "off"}:
            return False
        # 알 수 없는 문자열은 안전하게 OFF
        return False
    return bool(raw)


def _collect_weekly_rows_from_qdrant(
    *,
    start_ts: pd.Timestamp | None,
    end_ts: pd.Timestamp | None,
    kiwi: "Kiwi | None",
    stopwords: set[str],
    config: dict,
    logger,
) -> List[Dict[str, str | int]]:
    """Qdrant 뉴스 컬렉션에서 주차 범위를 전수 수집해 (week, keyword, source, count=1) 행을 만든다."""
    if start_ts is None or end_ts is None:
        raise ValueError(
            "Qdrant 입력 모드는 start_date/end_date(주차 범위)가 필요합니다. "
            "증분 실행 단위는 주차이며, 전체 재수집이 필요하면 범위를 명시하세요. "
            "(엑셀 입력으로 되돌리려면 config news.input_source=excel)"
        )
    from db.config import qdrant_url as _qdrant_url

    vcfg = config.get("vector_db", {})
    q_url = str(vcfg.get("qdrant_url") or _qdrant_url())
    collection = str(vcfg.get("collection", "news_10y_ko_v1"))
    date_field = str(vcfg.get("date_field", "date"))
    date_start_field = str(vcfg.get("date_start_field", "file_date_start"))
    date_end_field = str(vcfg.get("date_end_field", "file_date_end"))
    timeout_sec = float(vcfg.get("timeout_sec", 30))
    # 1단계는 기사 실제 발행일(date) 기준 정확 집계 → date-only 필터(파일오버랩 불필요).
    # date 필드 payload 인덱스 필요(0단계/인프라). docs/QDRANT_CONTRACT.md 참조.
    include_overlap = False

    weeks = weeks_in_range(start_ts, end_ts)
    logger.info("Qdrant 입력 | collection=%s | 주차수=%d | %s..%s", collection, len(weeks),
                weeks[0] if weeks else "-", weeks[-1] if weeks else "-")
    # 맥락(context) 부가 수집 설정 — 키워드 추출(weekly_keywords)은 불변.
    ctx_cfg = (config.get("keyword_extractor", {}) or {}).get("context", {}) or {}
    write_context = bool(ctx_cfg.get("enabled", False)) and kiwi is not None
    ctx_top_n = int(ctx_cfg.get("top_n", 2000))
    ctx_samples = int(ctx_cfg.get("samples", 3))
    ctx_nbr_k = int(ctx_cfg.get("neighbor_top_k", 5))
    sense_cfg = (config.get("keyword_extractor", {}) or {}).get("sense", {}) or {}
    # sense 문장 모드: 키워드 포함 "문장 전체"를 수집해 임베딩 군집(제목>본문, 앞문장 가중).
    sense_sentence = (
        bool(sense_cfg.get("enabled", False))
        and str(sense_cfg.get("input_mode", "sentence")) == "sentence"
        and kiwi is not None
    )
    s_title_w = float(sense_cfg.get("title_weight", 3.0))
    s_body_base = float(sense_cfg.get("body_base_weight", 1.0))
    s_body_decay = float(sense_cfg.get("body_position_decay", 0.85))
    s_min_w = float(sense_cfg.get("min_sentence_weight", 0.2))
    s_min_len = int(sense_cfg.get("min_sentence_len", 12))
    s_lead_n = int(sense_cfg.get("body_lead_n", 3))
    s_noise = set(sense_cfg.get("noise_keywords", []))  # sense 전용: 노이즈 키워드 제외(weekly_keywords 불변)

    rows: List[Dict[str, str | int]] = []
    for week in weeks:
        n_art = 0
        ctx_counter: Dict[str, Counter] = defaultdict(Counter)
        nbr_counter: Dict[str, Dict[str, Counter]] = defaultdict(
            lambda: {"before": Counter(), "after": Counter()}
        )
        sent_weight: Dict[str, float] = defaultdict(float)        # sense(대안 C): 문장 → 가중합
        sent_kws: Dict[str, Counter] = defaultdict(Counter)       # 문장 → {키워드: cnt} (라벨·매핑용)
        action_words: set[str] = set()                            # 액션(동사/동작명사) 단어 표지 → 라벨 조합용
        week_kw_count: Counter = Counter()
        for art in iter_week_articles(
            week, logger=logger, qdrant_url=q_url, collection=collection,
            date_field=date_field, date_start_field=date_start_field, date_end_field=date_end_field,
            timeout_sec=timeout_sec, include_file_overlap=include_overlap,
        ):
            n_art += 1
            source_val = art["source"] or "unknown"
            if write_context:
                norm = re.sub(r"\s+", " ", str(art["body"])).strip()
                noun_pos_full = _extract_nouns_with_pos(
                    norm, kiwi, stopwords,
                    include_person=sense_sentence, include_verbs=sense_sentence,
                )
                # weekly_keywords·맥락: 인물·동사 제외(불변). NNG 동작명사는 기존대로 유지.
                noun_pos = [(w, p) for w, p, isp, at in noun_pos_full if not isp and at != "verb"]
                _accumulate_context(norm, noun_pos, ctx_counter, nbr_counter)
                if sense_sentence:
                    sense_noun_pos = [(w, p, at) for w, p, _isp, at in noun_pos_full]  # sense: 인물·동사 포함 + 액션표지
                    _accumulate_sentences(
                        str(art.get("title") or ""), norm, sense_noun_pos, kiwi, stopwords,
                        sent_weight, sent_kws, action_words,
                        title_weight=s_title_w, body_base=s_body_base, body_decay=s_body_decay,
                        min_sent_w=s_min_w, min_sent_len=s_min_len, lead_n=s_lead_n,
                        noise_kw=s_noise,
                    )
                nouns = [w for w, _ in noun_pos]
            else:
                nouns = _extract_nouns(art["body"], kiwi, stopwords)
            for keyword in nouns:
                rows.append({"week": week, "keyword": keyword, "source": str(source_val), "count": 1})
                if write_context:
                    week_kw_count[keyword] += 1
        logger.info("Qdrant 주차 수집 | week=%s | articles=%d | 누적행=%d", week, n_art, len(rows))
        if write_context:
            _persist_week_context(
                week, week_kw_count, ctx_counter, nbr_counter,
                top_n=ctx_top_n, samples=ctx_samples, nbr_k=ctx_nbr_k,
                sense_cfg=sense_cfg, logger=logger,
                sent_weight=sent_weight, sent_kws=sent_kws, action_words=action_words,
            )
    return rows


def _persist_week_context(
    week: str,
    week_kw_count: Counter,
    ctx_counter: Dict[str, Counter],
    nbr_counter: Dict[str, Dict[str, Counter]],
    *,
    top_n: int,
    samples: int,
    nbr_k: int,
    sense_cfg: dict | None = None,
    logger,
    sent_weight: Dict[str, float] | None = None,
    sent_kws: Dict[str, Counter] | None = None,
    action_words: set[str] | None = None,
) -> None:
    """주차별 상위 top_n 빈도 키워드만 맥락(B 예문 top samples + C 주변어절 top nbr_k) 적재.

    sense_cfg.enabled 면 같은 top_kws 에 대해 의미 분화(sense)도 산출·적재한다.
    input_mode=="sentence" 면 문장 임베딩 군집(sent_counter/sent_co 사용), 아니면 어절 맥락 군집.
    """
    from db import repository as repo

    top_kws = [kw for kw, _ in week_kw_count.most_common(top_n)]
    ctx_rows: List[Tuple[str, str, int, str, str, int]] = []
    nbr_rows: List[Tuple[str, str, str, str, int]] = []
    for kw in top_kws:
        for rank, ((before, after), cnt) in enumerate(ctx_counter[kw].most_common(samples), start=1):
            ctx_rows.append((week, kw, rank, before or None, after or None, int(cnt)))
        nb = nbr_counter[kw]
        for term, cnt in nb["before"].most_common(nbr_k):
            nbr_rows.append((week, kw, "before", term, int(cnt)))
        for term, cnt in nb["after"].most_common(nbr_k):
            nbr_rows.append((week, kw, "after", term, int(cnt)))
    nc, nn = repo.replace_keyword_context(week, ctx_rows, nbr_rows)
    logger.info(
        "키워드 맥락 적재 | week=%s | 상위키워드=%d | context=%d행 | neighbor=%d행",
        week, len(top_kws), nc, nn,
    )

    if sense_cfg and bool(sense_cfg.get("enabled", False)):
        if str(sense_cfg.get("input_mode", "sentence")) == "sentence":
            _persist_week_sense_sentences(
                week, top_kws, sent_weight or {}, sent_kws or {}, sense_cfg, logger,
                action_words=action_words or set(),
            )
        else:
            _persist_week_sense(week, top_kws, ctx_counter, sense_cfg, logger)


def _persist_week_sense_sentences(
    week: str,
    top_kws: List[str],
    sent_weight: Dict[str, float],
    sent_kws: Dict[str, Counter],
    sense_cfg: dict,
    logger,
    action_words: set[str] | None = None,
) -> None:
    """의미 분화(sense) + 트렌드 — 대안 C(전역 문장 군집). 임베딩 실행 환경(맥미니) 필요."""
    from db import repository as repo
    from steps.keyword_sense import compute_week_trend_senses

    model = sense_cfg.get("model")
    device = sense_cfg.get("device", "auto")

    def embed_fn(sentences: List[str]):
        from steps.qdrant_embed import embed_texts
        return embed_texts(sentences, model_name=model, device=device, normalize=True)

    sense_rows, trend_rows = compute_week_trend_senses(
        week, top_kws, sent_weight, sent_kws,
        embed_fn=embed_fn, action_words=action_words or set(),
        cluster_target_size=int(sense_cfg.get("cluster_target_size", 40)),
        cluster_kmin=int(sense_cfg.get("cluster_kmin", 20)),
        cluster_kmax=int(sense_cfg.get("cluster_kmax", 400)),
        max_senses=int(sense_cfg.get("max_senses", 4)),
        neighbor_top_k=int(sense_cfg.get("neighbor_top_k", 5)),
        min_sense_share=float(sense_cfg.get("min_sense_share", 0.12)),
        logger=logger,
    )
    n_sense = repo.replace_keyword_sense(week, sense_rows)
    n_trend = repo.replace_trend_clusters(week, trend_rows)
    n_kw = len({r[1] for r in sense_rows})
    logger.info(
        "키워드 의미분화(트렌드) 적재 | week=%s | 키워드=%d | sense=%d행 | 트렌드=%d",
        week, n_kw, n_sense, n_trend,
    )


def _persist_week_sense(
    week: str,
    top_kws: List[str],
    ctx_counter: Dict[str, Counter],
    sense_cfg: dict,
    logger,
) -> None:
    """의미 분화(sense) 산출·적재 — 임베딩 군집(방법 B). 임베딩 실행 환경(맥미니) 필요."""
    from db import repository as repo
    from steps.keyword_sense import compute_week_senses

    model = sense_cfg.get("model")  # None 이면 기본 모델
    device = sense_cfg.get("device", "auto")

    def embed_fn(sentences: List[str]):
        from steps.qdrant_embed import embed_texts
        return embed_texts(sentences, model_name=model, device=device, normalize=True)

    sense_rows = compute_week_senses(
        week, top_kws, ctx_counter,
        embed_fn=embed_fn,
        distance_threshold=float(sense_cfg.get("distance_threshold", 0.35)),
        max_senses=int(sense_cfg.get("max_senses", 6)),
        neighbor_top_k=int(sense_cfg.get("neighbor_top_k", 5)),
        min_occ_for_split=int(sense_cfg.get("min_occ_for_split", 5)),
        min_contexts_for_split=int(sense_cfg.get("min_contexts_for_split", 3)),
        max_contexts_per_kw=int(sense_cfg.get("max_contexts_per_kw", 80)),
    )
    n_sense = repo.replace_keyword_sense(week, sense_rows)
    n_kw = len({r[1] for r in sense_rows})
    logger.info(
        "키워드 의미분화 적재 | week=%s | 키워드=%d | sense=%d행",
        week, n_kw, n_sense,
    )


def _finalize_weekly(
    weekly_rows: List[Dict[str, str | int]],
    *,
    output_dir: Path,
    logger,
    processed_files: int,
    start_date: str | None,
    end_date: str | None,
    use_feature_column_mode: bool,
    category_filter_values: List[str],
    write_to_db: bool,
) -> Dict[str, Path]:
    """공용 다운스트림: groupby → 병행 CSV 출력 → DB upsert."""
    result = pd.DataFrame(weekly_rows)
    if result.empty:
        raise ValueError("키워드 추출 결과가 비어 있습니다. (입력 범위/소스를 확인하세요)")

    result = (
        result.groupby(["week", "keyword", "source"], as_index=False)["count"]
        .sum()
        .sort_values(["week", "count"], ascending=[True, False])
    )

    # 주: 사용자 제외 키워드는 1단계에서 '삭제하지 않는다'(원천 데이터 보존).
    # 제외는 keyword_exclusions 플래그로 저장되고, 분석 단계(6 trend·7 clustering·8)에서만 필터한다.

    # DB 적재 — 처리 주차를 먼저 비우고(주차 replace) 재적재(멱등 재실행).
    # upsert 만으로는 고도화로 사라진 키워드의 old 행이 잔존하므로 DELETE 선행.
    if write_to_db:
        from db import repository as repo

        db_rows = list(result[["week", "keyword", "source", "count"]].itertuples(index=False, name=None))
        weeks_to_replace = sorted(result["week"].astype(str).unique().tolist())
        n_del = repo.delete_weekly_keywords(weeks_to_replace)
        logger.info("주차 replace | 기존 삭제=%d행 | weeks=%d", n_del, len(weeks_to_replace))
        n_db = repo.upsert_weekly_keywords(db_rows)
        logger.info("DB 적재 weekly_keywords | upsert=%d행", n_db)

    out_path = output_dir / "weekly_keywords.csv"
    written_path = write_csv(result, out_path)
    json_path = write_dataframe_json_export(
        result,
        written_path,
        step="keyword_extractor",
        extra_meta={
            "processed_files": processed_files,
            "start_date": start_date,
            "end_date": end_date,
            "use_feature_column_mode": use_feature_column_mode,
            "category_filter_values": category_filter_values,
        },
    )
    meta_path = written_path.with_suffix(".meta.json")
    logger.info(
        "keyword_extractor 요약 | 처리파일=%d | 행수=%d | csv=%s | json=%s",
        processed_files, len(result), written_path, json_path,
    )
    log_artifact(logger, "OUTPUT_CSV", written_path)
    log_artifact(logger, "OUTPUT_JSON", json_path)
    if meta_path.exists():
        log_artifact(logger, "OUTPUT_JSON_META", meta_path)
    weeks = sorted(result["week"].astype(str).unique().tolist())
    return {"csv": written_path, "json": json_path, "weeks": weeks}


def run_keyword_extractor(
    start_date: str | None = None,
    end_date: str | None = None,
    use_feature_column_mode: Optional[bool] = None,
    category_filter_values: Optional[List[str]] = None,
) -> Dict[str, Path]:
    logger = get_logger("steps.keyword_extractor")
    start_ts = _parse_iso_date("start_date", start_date)
    end_ts = _parse_iso_date("end_date", end_date)
    if start_ts is not None and end_ts is not None and start_ts > end_ts:
        raise ValueError("start_date가 end_date보다 늦습니다.")
    filter_end_inclusive = None
    if end_ts is not None:
        filter_end_inclusive = end_ts + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)

    with log_step(
        logger,
        1,
        "keyword_extractor",
        start_date=start_date,
        end_date=end_date,
    ):
        config = load_config()
        paths = config["paths"]
        news_cfg = config["news"]

        news_dir = resolve_path(paths["news_dir"])
        output_dir = ensure_output_dir()
        stopwords = _load_stopwords(resolve_path(paths["stopwords_path"]))
        kiwi = None
        if Kiwi is not None:
            try:
                kiwi = Kiwi(num_workers=0, model_type="sbg", load_default_dict=True)
                logger.info("Kiwi 초기화 완료 | model_type=sbg (강제)")
            except Exception as exc:
                raise RuntimeError(
                    "Kiwi sbg 모델 초기화에 실패했습니다. "
                    "sbg 모델 파일 설치/버전을 확인하세요."
                ) from exc

        # 모드 전환: 기본은 기존 명사 추출, 필요시 특성추출 컬럼 직접 사용
        if use_feature_column_mode is None:
            use_feature_column_mode = bool(news_cfg.get("use_feature_column_mode", False))
        if category_filter_values is None:
            category_filter_values = list(news_cfg.get("category_filter_values", []))
        category_filter_values = [str(v).strip() for v in category_filter_values if str(v).strip()]
        logger.info(
            "키워드 모드 | use_feature_column_mode=%s | category_filter_values=%s",
            use_feature_column_mode,
            category_filter_values,
        )

        # 입력 소스 분기: 기본 Qdrant(증분), 폴백 excel
        input_source = str(news_cfg.get("input_source", "qdrant")).strip().lower()
        logger.info("입력 소스 | input_source=%s", input_source)
        if input_source == "qdrant":
            weekly_rows = _collect_weekly_rows_from_qdrant(
                start_ts=start_ts,
                end_ts=end_ts,
                kiwi=kiwi,
                stopwords=stopwords,
                config=config,
                logger=logger,
            )
            return _finalize_weekly(
                weekly_rows,
                output_dir=output_dir,
                logger=logger,
                processed_files=0,
                start_date=start_date,
                end_date=end_date,
                use_feature_column_mode=use_feature_column_mode,
                category_filter_values=category_filter_values,
                write_to_db=True,
            )

        files = sorted(news_dir.glob(news_cfg["excel_pattern"]))
        if not files:
            raise FileNotFoundError(f"뉴스 파일을 찾을 수 없습니다: {news_dir}")
        logger.info("입력 파일 수: %d", len(files))

        weekly_rows: List[Dict[str, str | int]] = []
        processed_files = 0
        files_opened = 0
        skipped_by_filename = 0
        skipped_filename_unparsed = 0
        skipped_row_filter = 0
        empty_after_nouns = 0

        has_user_range = (start_ts is not None) or (filter_end_inclusive is not None)
        filename_filter_enabled = _resolve_filename_filter_enabled(
            news_cfg.get("filename_date_filter", "auto"),
            has_user_range=has_user_range,
        )
        filename_pattern = str(
            news_cfg.get("filename_date_pattern", r"^NewsResult_(\d{8})-(\d{8})\.xlsx$")
        )
        dataset_range = _scan_filename_dataset_range(files, filename_pattern)
        if dataset_range is not None:
            logger.info(
                "파일명 기준 데이터셋 기간(추정) | min=%s | max=%s | (파일명 구간의 union)",
                dataset_range[0].date(),
                dataset_range[1].date(),
            )
        logger.info(
            "파일명 기반 사전필터 | enabled=%s | user_range=%s~%s",
            filename_filter_enabled,
            start_ts.date() if start_ts is not None else None,
            end_ts.date() if end_ts is not None else None,
        )

        for excel_path in files:
            if filename_filter_enabled and (start_ts is not None or filter_end_inclusive is not None):
                file_range = _parse_filename_range(excel_path, filename_pattern)
                if file_range is None:
                    skipped_filename_unparsed += 1
                    logger.debug("파일명 날짜 구간 파싱 실패(내용 읽기로 진행): %s", excel_path.name)
                else:
                    f_start, f_end = file_range
                    if not _ranges_overlap(start_ts, filter_end_inclusive, f_start, f_end):
                        skipped_by_filename += 1
                        logger.debug(
                            "파일명 기간과 사용자 기간이 겹치지 않아 스킵(엑셀 미오픈) | file=%s | file_range=%s~%s | user_range=%s~%s",
                            excel_path.name,
                            f_start.date(),
                            f_end.date(),
                            start_ts.date() if start_ts is not None else None,
                            end_ts.date() if end_ts is not None else None,
                        )
                        continue

            logger.info("파일 처리 중: %s", excel_path.name)
            df = pd.read_excel(excel_path)
            files_opened += 1
            if df.empty:
                logger.warning("빈 파일 건너뜀: %s", excel_path.name)
                continue

            text_col = _pick_column(df.columns.tolist(), news_cfg["candidate_text_columns"])
            source_col = _pick_column(df.columns.tolist(), news_cfg["source_columns"])
            date_col = _pick_column(
                df.columns.tolist(), ["date", "날짜", "일자", "published_at", "등록일", "작성일"]
            )
            category_col = _pick_column(df.columns.tolist(), ["통합 분류1", "통합분류1", "category1"])
            feature_col = _pick_column(
                df.columns.tolist(),
                ["특성추출(가중치순 상위 50개)", "특성추출", "feature_top50", "feature_keywords"],
            )

            if use_feature_column_mode:
                if feature_col is None:
                    logger.warning("특성추출 컬럼 미탐지로 건너뜀: %s", excel_path.name)
                    continue
            elif text_col is None:
                logger.warning("본문 컬럼 미탐지로 건너뜀: %s", excel_path.name)
                continue
            if date_col is None:
                # 파일명에서 기간이 제공되어도 주차 집계를 위해 최소한의 날짜열이 필요함
                logger.warning("날짜 컬럼 미탐지로 건너뜀: %s", excel_path.name)
                continue

            selected_cols = [date_col, source_col, category_col]
            if use_feature_column_mode:
                selected_cols.append(feature_col)
            else:
                selected_cols.append(text_col)
            tmp = df[[c for c in selected_cols if c is not None]].copy()
            tmp["__dt"] = _coerce_news_date_series(tmp[date_col])
            before_rows = int(len(tmp))
            required_text_col = feature_col if use_feature_column_mode else text_col
            tmp = tmp.dropna(subset=["__dt", required_text_col])
            after_parse_rows = int(len(tmp))

            if start_ts is not None:
                tmp = tmp[tmp["__dt"] >= start_ts]
            if end_ts is not None:
                # inclusive end date (day-level)
                tmp = tmp[tmp["__dt"] <= end_ts + pd.Timedelta(days=1) - pd.Timedelta(nanoseconds=1)]

            if use_feature_column_mode and category_filter_values:
                if category_col is None:
                    logger.warning("통합분류1 컬럼 미탐지로 건너뜀: %s", excel_path.name)
                    continue
                tmp = tmp[
                    tmp[category_col]
                    .astype(str)
                    .apply(lambda x: any(v in x for v in category_filter_values))
                ]

            after_filter_rows = int(len(tmp))
            if after_filter_rows == 0:
                skipped_row_filter += 1
                logger.debug(
                    "행 단위 기간 필터로 인해 데이터 없음(건너뜀) | file=%s | before_rows=%d | parsed_rows=%d | filtered_rows=%d",
                    excel_path.name,
                    before_rows,
                    after_parse_rows,
                    after_filter_rows,
                )
                continue

            tmp["week"] = _iso_week_label(tmp["__dt"])

            file_had_output = False
            for _, row in tmp.iterrows():
                if use_feature_column_mode:
                    nouns = _extract_feature_keywords(row[feature_col], stopwords)
                else:
                    nouns = _extract_nouns(row[text_col], kiwi, stopwords)
                source_val = row[source_col] if source_col else "unknown"
                for keyword in nouns:
                    file_had_output = True
                    weekly_rows.append(
                        {
                            "week": row["week"],
                            "keyword": keyword,
                            "source": str(source_val),
                            "count": 1,
                        }
                    )
            if file_had_output:
                processed_files += 1
            else:
                empty_after_nouns += 1
                logger.debug("명사 추출 결과가 비어 파일 스킵: %s", excel_path.name)

        logger.info(
            "파일 스킵 요약 | filename_skipped=%d | filename_unparsed=%d | rowfilter_skipped=%d | opened=%d | noun_empty=%d | processed=%d",
            skipped_by_filename,
            skipped_filename_unparsed,
            skipped_row_filter,
            files_opened,
            empty_after_nouns,
            processed_files,
        )

        if not weekly_rows:
            hint = ""
            if dataset_range is not None and (start_ts is not None or end_ts is not None):
                hint = (
                    f" 파일명 기준 추정 데이터 범위는 {dataset_range[0].date()}~{dataset_range[1].date()} 입니다."
                    " 사용자 기간과 맞지 않으면 결과가 비어있을 수 있습니다."
                    " `config/pipeline_config.json`의 `news.filename_date_filter`를 false로 두거나,"
                    " 기간을 데이터 범위에 맞게 조정하세요."
                )
            raise ValueError(
                "키워드 추출 결과가 비어 있습니다."
                f" (opened_files={files_opened}, filename_skipped={skipped_by_filename}, rowfilter_skipped={skipped_row_filter}, noun_empty_files={empty_after_nouns})"
                + hint
            )

        return _finalize_weekly(
            weekly_rows,
            output_dir=output_dir,
            logger=logger,
            processed_files=processed_files,
            start_date=start_date,
            end_date=end_date,
            use_feature_column_mode=use_feature_column_mode,
            category_filter_values=category_filter_values,
            write_to_db=True,
        )
