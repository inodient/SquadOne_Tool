from __future__ import annotations

from dataclasses import dataclass
import json
import os
from typing import Protocol
from urllib import request


class LLMClient(Protocol):
    def invoke(self, prompt: str) -> str:
        ...

    def get_last_error(self) -> str | None:
        ...


@dataclass(slots=True)
class MockLLMClient:
    role: str
    _last_error: str | None = None

    def invoke(self, prompt: str) -> str:
        self._last_error = None
        return (
            f"[{self.role}] "
            "요약: 벡터DB 근거를 기반으로 이번 주 트렌드가 지속/변화했습니다."
        )

    def get_last_error(self) -> str | None:
        return self._last_error


@dataclass(slots=True)
class OllamaLLMClient:
    model: str
    base_url: str
    temperature: float
    _last_error: str | None = None

    def invoke(self, prompt: str) -> str:
        try:
            self._last_error = None
            payload = {
                "model": self.model,
                "prompt": prompt,
                "stream": False,
                "options": {"temperature": self.temperature},
            }
            body = json.dumps(payload).encode("utf-8")
            req = request.Request(
                url=self.base_url.rstrip("/") + "/api/generate",
                data=body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with request.urlopen(req, timeout=180) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            out = str(data.get("response", "")).strip()
            if out:
                return out
            self._last_error = f"ollama empty response (model={self.model})"
            return ""
        except Exception as exc:
            self._last_error = f"ollama invoke failed (model={self.model}, base_url={self.base_url}): {exc}"
            return ""

    def get_last_error(self) -> str | None:
        return self._last_error


@dataclass(slots=True)
class GeminiLLMClient:
    model: str
    api_key: str
    _last_error: str | None = None

    def invoke(self, prompt: str) -> str:
        try:
            self._last_error = None
            import google.generativeai as genai

            genai.configure(api_key=self.api_key)
            model = genai.GenerativeModel(self.model)
            response = model.generate_content(prompt)
            out = str(getattr(response, "text", "")).strip()
            if out:
                return out
            self._last_error = f"gemini empty response (model={self.model})"
            return ""
        except Exception as exc:
            self._last_error = f"gemini invoke failed (model={self.model}): {exc}"
            return ""

    def get_last_error(self) -> str | None:
        return self._last_error


def _model_for_role(role: str) -> str:
    role_upper = role.upper()
    fallback = os.getenv("OLLAMA_MODEL_DEFAULT", "llama3.1:8b")
    return os.getenv(f"OLLAMA_MODEL_{role_upper}", fallback)


def get_llm(role: str) -> LLMClient:
    provider = os.getenv("LLM_PROVIDER", "ollama").lower()
    if provider == "mock":
        return MockLLMClient(role=role)
    if provider == "gemini":
        api_key = os.getenv("GEMINI_API_KEY", "").strip()
        if not api_key:
            return MockLLMClient(role=role)
        model = os.getenv("GEMINI_MODEL_DEFAULT", "gemini-1.5-flash")
        return GeminiLLMClient(model=model, api_key=api_key)

    model = _model_for_role(role)
    base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    temperature = float(os.getenv("OLLAMA_TEMPERATURE", "0.1"))
    return OllamaLLMClient(model=model, base_url=base_url, temperature=temperature)
