"""Append-only JSON history under sandbox/history/{run_id}.json."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.config import AppConfig


def _utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def history_path(config: AppConfig, run_id: str) -> Path:
    return config.history_dir / f"{run_id}.json"


def load_history(config: AppConfig, run_id: str) -> dict[str, Any]:
    path = history_path(config, run_id)
    if not path.exists():
        return {
            "run_id": run_id,
            "url": "",
            "objective": "",
            "created_at": _utc_iso(),
            "generations": [],
        }
    return json.loads(path.read_text(encoding="utf-8"))


def ensure_history(
    config: AppConfig,
    run_id: str,
    url: str,
    objective: str,
) -> dict[str, Any]:
    path = history_path(config, run_id)
    config.history_dir.mkdir(parents=True, exist_ok=True)
    if path.exists():
        data = load_history(config, run_id)
        data.setdefault("generations", [])
        return data
    data = {
        "run_id": run_id,
        "url": url,
        "objective": objective,
        "created_at": _utc_iso(),
        "generations": [],
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return data


def append_generation(
    config: AppConfig,
    run_id: str,
    *,
    planner_summary: str = "",
    code_path: str = "",
    critic_feedback: str = "",
    extra: dict[str, Any] | None = None,
) -> None:
    path = history_path(config, run_id)
    data = load_history(config, run_id) if path.exists() else ensure_history(config, run_id, "", "")
    gen: dict[str, Any] = {
        "ts": _utc_iso(),
        "planner_summary": planner_summary,
        "code_path": code_path,
        "critic_feedback": critic_feedback,
    }
    if extra:
        gen.update(extra)
    data.setdefault("generations", []).append(gen)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def set_last_generation_critic_feedback(config: AppConfig, run_id: str, feedback: str) -> None:
    path = history_path(config, run_id)
    if not path.exists():
        return
    data = load_history(config, run_id)
    gens = data.get("generations") or []
    if not gens:
        return
    gens[-1]["critic_feedback"] = feedback
    data["generations"] = gens
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def update_history_meta(
    config: AppConfig,
    run_id: str,
    **fields: Any,
) -> None:
    path = history_path(config, run_id)
    data = load_history(config, run_id) if path.exists() else {"run_id": run_id, "generations": []}
    for k, v in fields.items():
        data[k] = v
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
