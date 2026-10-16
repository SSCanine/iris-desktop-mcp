"""Unit tests for spatial.current_bounds + token.current_bounds()."""

from __future__ import annotations

import pytest

from iris import spatial as spatial_mod
from iris.geometry import Rect
from iris.tokens import FocusToken


def test_current_bounds_returns_none_for_dead_hwnd():
    # Hwnd 0 is never a valid window.
    assert spatial_mod.current_bounds(0) is None


def test_current_bounds_returns_rect_for_live_window():
    # Find ANY live window from the enumeration. If there are none, we can't
    # exercise this without spawning a window; just skip.
    pool = spatial_mod.enumerate_windows()
    if not pool:
        pytest.skip("no live windows to probe")
    live = pool[0]
    bounds = spatial_mod.current_bounds(live.hwnd)
    assert bounds is not None
    assert isinstance(bounds, Rect)
    # The live read should match what enumerate_windows reported, modulo any
    # in-flight repaint (window is stable across these two calls).
    assert bounds == live.bounds


def test_token_current_bounds_proxies_to_spatial():
    pool = spatial_mod.enumerate_windows()
    if not pool:
        pytest.skip("no live windows to probe")
    live = pool[0]
    tk = FocusToken.create(
        hwnd=live.hwnd,
        pid=live.pid,
        exe_name=live.exe_name,
        title=live.title,
        monitor_index=0,
        bounds=live.bounds,
    )
    assert tk.current_bounds() == spatial_mod.current_bounds(live.hwnd)


def test_token_current_bounds_returns_none_for_dead_token():
    tk = FocusToken.create(
        hwnd=0,
        pid=0,
        exe_name="dead",
        title="",
        monitor_index=0,
        bounds=Rect(0, 0, 0, 0),
    )
    assert tk.current_bounds() is None
