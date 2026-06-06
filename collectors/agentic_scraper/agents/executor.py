"""Run generated script in a subprocess with cwd and env; collect outputs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

from core.config import AppConfig
from core.state import AgentState


def _enforce_output_schema(data: dict[str, Any], objective: str, url: str) -> dict[str, Any]:
    if not isinstance(data, dict):
        return {"_schema_error": "result is not a JSON object", "items": [], "meta": {"source_url": url}}
    items = data.get("items")
    if not isinstance(items, list):
        return {"_schema_error": "items must be a list", "items": [], "meta": {"source_url": url}}

    normalized_items: list[dict[str, str]] = []
    for idx, item in enumerate(items):
        if not isinstance(item, dict):
            return {
                "_schema_error": f"items[{idx}] is not an object",
                "items": [],
                "meta": {"source_url": url},
            }
        title = str(item.get("title", "")).strip()
        source_url = str(item.get("source_url", "")).strip()
        selector = str(item.get("selector", "")).strip()
        raw_snippet = str(item.get("raw_snippet", "")).strip()
        if not (title and source_url and selector and raw_snippet):
            return {
                "_schema_error": f"items[{idx}] missing required fields",
                "items": [],
                "meta": {"source_url": url},
            }
        normalized_items.append(
            {
                "title": title,
                "source_url": source_url,
                "selector": selector,
                "raw_snippet": raw_snippet,
            }
        )

    meta = data.get("meta") if isinstance(data.get("meta"), dict) else {}
    section = str(meta.get("section", "")).strip()
    source_url = str(meta.get("source_url", "")).strip() or url
    return {
        "items": normalized_items,
        "meta": {
            "section": section,
            "source_url": source_url,
            "objective": objective,
        },
    }


def run_executor(state: AgentState, app: AppConfig) -> dict[str, Any]:
    run_id = state.get("run_id") or ""
    script = Path(state.get("generated_script_path") or app.generated_script_path)
    app.outputs_dir.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["RUN_ID"] = run_id
    env["OUTPUT_DIR"] = str(app.outputs_dir)
    env["PROJECT_ROOT"] = str(app.project_root)

    proc = subprocess.run(
        [sys.executable, str(script)],
        cwd=str(app.project_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=app.executor_timeout_sec,
        check=False,
    )
    result_path = app.outputs_dir / f"{run_id}_result.json"
    final_data: dict[str, Any] = {}
    objective = str(state.get("objective") or "")
    url = str(state.get("url") or "")
    if result_path.exists():
        try:
            parsed = json.loads(result_path.read_text(encoding="utf-8"))
            final_data = _enforce_output_schema(parsed, objective, url)
        except json.JSONDecodeError:
            final_data = {"_parse_error": True, "raw": result_path.read_text(encoding="utf-8")[:2000]}
    else:
        final_data = {"_schema_error": "result file not created", "items": [], "meta": {"source_url": url}}

    shots: list[str] = []
    if proc.returncode != 0:
        diag = app.outputs_dir / f"{run_id}_executor_error.txt"
        diag.write_text(proc.stderr or proc.stdout or "", encoding="utf-8")
        shots.append(str(diag))

    return {
        "execution_stdout": proc.stdout or "",
        "execution_stderr": proc.stderr or "",
        "final_data": final_data,
        "screenshot_paths": shots,
    }
