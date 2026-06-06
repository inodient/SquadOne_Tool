"""Shared fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest

from core.config import AppConfig, get_config


@pytest.fixture
def tmp_app(tmp_path: Path) -> AppConfig:
    root = tmp_path
    sb = root / "sandbox"
    hist = sb / "history"
    out = sb / "outputs"
    hist.mkdir(parents=True)
    out.mkdir(parents=True)
    return get_config(
        overrides={
            "project_root": root,
            "sandbox_dir": sb,
            "data_dir": root / "data",
            "history_dir": hist,
            "outputs_dir": out,
            "generated_script_path": sb / "generated_script.py",
            "mock_llm": True,
            "skip_browser": True,
            "max_retries": 2,
        }
    )
