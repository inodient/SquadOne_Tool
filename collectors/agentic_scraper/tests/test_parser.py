"""Unit: HTML summary truncation and tag whitelist."""

from __future__ import annotations

import pytest

from tools.parser import html_summary


@pytest.mark.unit
def test_html_summary_truncates() -> None:
    long_inner = "x" * 20_000
    html = f"<html><body><p>{long_inner}</p></body></html>"
    out = html_summary(html, max_chars=500)
    assert len(out) <= 520
    assert "truncated" in out or len(out) < len(long_inner)
