"""Unit: history JSON append."""

from __future__ import annotations

import json

import pytest

from core.history import append_generation, ensure_history, history_path, load_history


@pytest.mark.unit
def test_history_append(tmp_app) -> None:
    app = tmp_app
    rid = "run-test-1"
    ensure_history(app, rid, "https://x.test", "obj")
    append_generation(app, rid, planner_summary="p", code_path="/tmp/x.py", critic_feedback="")
    append_generation(app, rid, planner_summary="p2", code_path="/tmp/y.py", critic_feedback="bad")
    p = history_path(app, rid)
    data = json.loads(p.read_text(encoding="utf-8"))
    assert len(data["generations"]) == 2
    assert data["generations"][1]["code_path"] == "/tmp/y.py"


@pytest.mark.unit
def test_load_history_missing(tmp_app) -> None:
    app = tmp_app
    h = load_history(app, "missing")
    assert h["run_id"] == "missing"
    assert h["generations"] == []
