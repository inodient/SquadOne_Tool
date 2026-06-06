"""Persist final_data to data/ on success; align history_record_id."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any

from core.config import AppConfig
from core.history import update_history_meta
from core.state import AgentState


def run_finalize(state: AgentState, app: AppConfig) -> dict[str, Any]:
    run_id = state.get("run_id") or ""
    app.data_dir.mkdir(parents=True, exist_ok=True)
    rid = run_id or "unknown"
    if state.get("success"):
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        path = app.data_dir / f"{rid}_{ts}.json"
        path.write_text(
            json.dumps(state.get("final_data") or {}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        update_history_meta(app, rid, exported_data_path=str(path), completed_success=True)
        return {"history_record_id": str(path.resolve())}

    update_history_meta(app, rid, completed_success=False)
    return {"history_record_id": ""}
