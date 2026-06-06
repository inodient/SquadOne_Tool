"""Live cases fixture file is valid YAML (skipped unless live marker)."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml


@pytest.mark.unit
def test_live_cases_yaml_schema() -> None:
    p = Path(__file__).parent / "fixtures" / "live_cases.yaml"
    doc = yaml.safe_load(p.read_text(encoding="utf-8"))
    assert isinstance(doc, list)
    for case in doc:
        assert "name" in case
        assert "url" in case


@pytest.mark.live
def test_live_smoke_optional_env() -> None:
    import os
    import urllib.request

    from agents.base import BaseAgent
    from core.config import get_config

    url = os.environ.get("LIVE_TEST_URL")
    objective = os.environ.get("LIVE_TEST_OBJECTIVE")
    if not url or not objective:
        pytest.skip("LIVE_TEST_URL and LIVE_TEST_OBJECTIVE not set")
    ollama_base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    try:
        with urllib.request.urlopen(f"{ollama_base}/api/tags", timeout=2):
            pass
    except Exception:  # noqa: BLE001
        pytest.skip("Ollama server unavailable; set OLLAMA_BASE_URL or run `ollama serve`")
    cfg = get_config(overrides={"mock_llm": False, "skip_browser": False})
    state = BaseAgent(cfg).run({"url": url, "objective": objective})
    assert state.get("run_id")
