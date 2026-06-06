"""Chat model factory: supports Ollama, Gemini, Anthropic, and a deterministic fake for tests."""

from __future__ import annotations

from typing import Any

from langchain_core.callbacks import CallbackManagerForLLMRun
from langchain_core.language_models.chat_models import BaseChatModel, SimpleChatModel
from langchain_core.messages import BaseMessage

from core.config import AppConfig


class QueueFakeChatModel(SimpleChatModel):
    """Consumes queued string responses in order (no cycling); last string repeats when empty."""

    queue: list[str]

    @property
    def _llm_type(self) -> str:
        return "queue-fake-chat"

    def _call(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> str:
        if self.queue:
            return self.queue.pop(0)
        return "{}"


def default_fake_responses() -> list[str]:
    """Planner JSON, selector JSON, critic JSON — one full loop (legacy pipeline)."""
    return [
        '{"plan_steps": ["Load page", "Extract title", "Save result"], "summary": "mock plan"}',
        '{"sports_tab_selector": "", "article_selector": "body", "title_link_selector": "h1", "meta_section": "sports"}',
        '{"success": true, "feedback": "mock critic ok"}',
    ]


def get_chat_model(config: AppConfig, fake_responses: list[str] | None = None) -> BaseChatModel:
    """Return the appropriate LangChain chat model based on config.llm_provider.

    mock_llm=True always returns QueueFakeChatModel regardless of provider,
    keeping existing pipeline tests working without modification.
    """
    if config.mock_llm:
        return QueueFakeChatModel(queue=list(fake_responses or default_fake_responses()))

    provider = (config.llm_provider or "ollama").lower()

    if provider == "gemini":
        import os
        from langchain_google_genai import ChatGoogleGenerativeAI  # type: ignore[import]

        # LLM_MODEL is shared across providers; prefer GEMINI_MODEL when using Gemini
        gemini_model = (
            os.getenv("GEMINI_MODEL")
            or (config.llm_model if config.llm_model != "llama3.1:8b" else None)
            or "gemini-2.0-flash"
        )
        return ChatGoogleGenerativeAI(
            model=gemini_model,
            google_api_key=config.gemini_api_key,  # type: ignore[arg-type]
            temperature=0,
        )

    if provider == "anthropic":
        from langchain_anthropic import ChatAnthropic  # type: ignore[import]

        return ChatAnthropic(
            model=config.llm_model or "claude-sonnet-4-6",
            anthropic_api_key=config.anthropic_api_key,  # type: ignore[arg-type]
            temperature=0,
            max_tokens=4096,
        )

    if provider == "ollama":
        from langchain_ollama import ChatOllama

        kwargs: dict[str, Any] = {"model": config.llm_model or "llama3.1:8b"}
        if config.ollama_base_url:
            kwargs["base_url"] = config.ollama_base_url
        return ChatOllama(**kwargs)

    raise ValueError(
        f"Unknown LLM_PROVIDER={provider!r}. Valid choices: ollama, gemini, anthropic"
    )
