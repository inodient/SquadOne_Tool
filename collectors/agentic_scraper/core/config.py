"""Application settings resolved from environment and project paths."""

from __future__ import annotations

from pathlib import Path

from typing import Self

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _resolve_under_root(path: Path | str, root: Path) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    return (root / p).resolve()


class AppConfig(BaseSettings):
    """Single source for API keys, model names, timeouts, and sandbox paths."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    project_root: Path = Field(default=_PROJECT_ROOT)

    # LLM provider selection: "ollama" | "gemini" | "anthropic"
    llm_provider: str = Field(default="ollama")
    ollama_base_url: str | None = None
    llm_model: str = "llama3.1:8b"
    gemini_api_key: str | None = Field(default=None)
    anthropic_api_key: str | None = Field(default=None)
    mock_llm: bool = False
    skip_browser: bool = False

    max_retries: int = Field(default=3, ge=0)
    generator_llm_retries: int = Field(default=3, ge=1, le=10)
    browser_headless: bool = True
    browser_slow_mo_ms: int = Field(default=0, ge=0)  # ms delay between actions (0 = off)
    browser_timeout_ms: int = Field(default=30_000, ge=1_000)
    executor_timeout_sec: int = Field(default=120, ge=1)

    sandbox_dir: Path = Field(default_factory=lambda: _PROJECT_ROOT / "sandbox")
    data_dir: Path = Field(default_factory=lambda: _PROJECT_ROOT / "data")
    history_dir: Path = Field(default_factory=lambda: _PROJECT_ROOT / "sandbox" / "history")
    outputs_dir: Path = Field(default_factory=lambda: _PROJECT_ROOT / "sandbox" / "outputs")
    generated_script_path: Path = Field(
        default_factory=lambda: _PROJECT_ROOT / "sandbox" / "generated_script.py"
    )

    # ReAct agent settings
    react_max_steps: int = Field(default=20, ge=1)
    react_step_delay_sec: float = Field(default=3.0, ge=0.0)  # delay between LLM calls (rate limit)
    react_outputs_dir: Path = Field(default_factory=lambda: _PROJECT_ROOT / "data" / "react_runs")

    # Selector cache settings
    cache_dir: Path = Field(default_factory=lambda: _PROJECT_ROOT / "data" / "cache")
    cache_stale_days: int = Field(default=7, ge=1)
    cache_max_failures: int = Field(default=3, ge=1)

    # Batch execution settings
    batch_max_workers: int = Field(default=4, ge=1, le=32)

    live_test_url: str | None = None
    live_test_objective: str | None = None

    @model_validator(mode="after")
    def _resolve_paths(self) -> Self:
        root = self.project_root
        self.sandbox_dir = _resolve_under_root(self.sandbox_dir, root)
        self.data_dir = _resolve_under_root(self.data_dir, root)
        self.history_dir = _resolve_under_root(self.history_dir, root)
        self.outputs_dir = _resolve_under_root(self.outputs_dir, root)
        self.generated_script_path = _resolve_under_root(self.generated_script_path, root)
        self.react_outputs_dir = _resolve_under_root(self.react_outputs_dir, root)
        self.cache_dir = _resolve_under_root(self.cache_dir, root)
        return self


def get_config(overrides: dict | None = None) -> AppConfig:
    if overrides:
        return AppConfig(**overrides)
    return AppConfig()
