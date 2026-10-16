"""End-to-end resolver tests against the Tkinter test harness.

The harness uses Tkinter, which does NOT expose ttk.Button widgets via UIA.
This is intentional: it lets us validate the OCR fallback path while still
having UIA work for native window controls (title bar Minimize/Maximize/Close).
"""

import pytest

from iris import resolver as resolver_mod
from iris.semantic import HAS_UIA
from iris.spatial import _make_window_info, get_monitor_for_window
from iris.tokens import FocusToken


def _make_token_from_hwnd(hwnd: int) -> FocusToken:
    info = _make_window_info(hwnd)
    assert info is not None
    monitor = get_monitor_for_window(info.bounds)
    return FocusToken.create(
        hwnd=info.hwnd,
        pid=info.pid,
        exe_name=info.exe_name,
        title=info.title,
        monitor_index=max(monitor, 0),
        bounds=info.bounds,
    )


@pytest.mark.skipif(not HAS_UIA, reason="UIA only")
def test_resolver_finds_titlebar_button_via_uia(iris_harness):
    """Title bar buttons (Minimize/Close) ARE exposed via UIA. Backend should be 'uia'."""
    token = _make_token_from_hwnd(iris_harness.hwnd)
    result = resolver_mod.find(token, "Close")
    assert result.found
    assert result.backend == "uia"
    assert "uia" in result.backends_tried
    assert len(result.hits) >= 1


def test_resolver_falls_back_to_ocr_for_tkinter_buttons(iris_harness):
    """Tkinter ttk.Button widgets are NOT in UIA. Resolver must fall back to OCR."""
    token = _make_token_from_hwnd(iris_harness.hwnd)
    result = resolver_mod.find(token, "Click Me", threshold=0.6)
    # Either UIA finds it (rare for Tkinter) or OCR does
    assert result.found, (
        f"Neither backend found 'Click Me'. Tried: {result.backends_tried}. Notes: {result.notes}"
    )
    assert result.backend in ("uia", "ocr")
    assert len(result.hits) >= 1


def test_resolver_handoff_when_target_missing(iris_harness):
    """Nonexistent target: returns vision_handoff with screenshot + notes."""
    token = _make_token_from_hwnd(iris_harness.hwnd)
    result = resolver_mod.find(token, "ThisButtonDoesNotExist12345", threshold=0.7)
    assert not result.found
    assert result.backend == "vision_handoff"
    assert result.screenshot is not None
    assert result.notes


def test_suggest_alternatives_returns_candidates(iris_harness):
    token = _make_token_from_hwnd(iris_harness.hwnd)
    result = resolver_mod.suggest_alternatives(token, "Close")
    assert "candidates" in result
    # Should at least find the title bar Close button
    assert len(result["candidates"]) >= 1
    top = result["candidates"][0]
    assert "score" in top
