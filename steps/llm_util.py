"""LLM 보조 유틸 — llm_factory 출력에서 JSON 안전 추출.

여러 인리치먼트 단계(geo_query/demand/market/social)가 LLM 으로부터 JSON 을 받는다.
LLM 출력은 코드펜스·잡설이 섞일 수 있어 견고한 파싱이 필요하다.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

from steps.llm_factory import get_llm


def extract_json(text: str) -> Optional[Any]:
    """문자열에서 첫 JSON 객체/배열을 추출(코드펜스/잡설 허용)."""
    if not text:
        return None
    s = text.strip()
    # ```json ... ``` 펜스 제거
    fence = re.search(r"```(?:json)?\s*(.+?)```", s, re.DOTALL)
    if fence:
        s = fence.group(1).strip()
    try:
        return json.loads(s)
    except Exception:  # noqa: BLE001
        pass
    # 첫 { ... } 또는 [ ... ] 블록 시도
    for pat in (r"\{.*\}", r"\[.*\]"):
        m = re.search(pat, s, re.DOTALL)
        if m:
            try:
                return json.loads(m.group(0))
            except Exception:  # noqa: BLE001
                continue
    return None


def invoke_json(role: str, prompt: str) -> Optional[Any]:
    """llm_factory(role) 호출 후 JSON 파싱(실패 시 None)."""
    try:
        out = get_llm(role).invoke(prompt)
    except Exception:  # noqa: BLE001
        return None
    return extract_json(out)
