"""Tests for the off_screen flag added to tokens.inspect()."""

import pytest

from iris import spatial as spatial_mod
from iris import tokens as tokens_mod
from iris.geometry import Rect
from iris.spatial import WindowInfo
from iris.tokens import FocusToken


def _token():
    return FocusToken.create(
        hwnd=123,
        pid=1,
        exe_name="x.exe",
        title="X",
        monitor_index=0,
        bounds=Rect(100, 100, 800, 600),
    )


def _stub_spatial(monkeypatch, *, info, monitors):
    """Patch the spatial helpers that inspect() lazily imports so we can
    assert off_screen logic without needing a real window."""
    if not spatial_mod.HAS_WIN32:
        pytest.skip("Win32 not available")
    monkeypatch.setattr(tokens_mod, "revalidate", lambda tk: True)
    monkeypatch.setattr(spatial_mod, "_make_window_info", lambda h: info)
    monkeypatch.setattr(spatial_mod, "list_monitors", lambda force_refresh=False: monitors)
    monkeypatch.setattr(spatial_mod, "get_monitor_for_window", lambda b, monitors=None: 0)
    monkeypatch.setattr(spatial_mod, "is_occluded", lambda h: False)
    monkeypatch.setattr(spatial_mod, "find_popups_for", lambda pid, exclude_hwnd=None: [])


def test_inspect_off_screen_true_when_bounds_outside_all_monitors(monkeypatch):
    info = WindowInfo(
        hwnd=123,
        pid=1,
        exe_name="x.exe",
        title="X",
        bounds=Rect(-50000, -50000, 100, 100),
        visible=True,
        minimized=False,
    )
    _stub_spatial(monkeypatch, info=info, monitors=[Rect(0, 0, 1920, 1080)])
    out = tokens_mod.inspect(_token())
    assert out["off_screen"] is True


def test_inspect_off_screen_false_when_bounds_intersect_monitor(monkeypatch):
    info = WindowInfo(
        hwnd=123,
        pid=1,
        exe_name="x.exe",
        title="X",
        bounds=Rect(500, 500, 100, 100),
        visible=True,
        minimized=False,
    )
    _stub_spatial(monkeypatch, info=info, monitors=[Rect(0, 0, 1920, 1080)])
    out = tokens_mod.inspect(_token())
    assert out["off_screen"] is False


def test_inspect_off_screen_handles_partial_intersection_with_secondary_monitor(monkeypatch):
    """A window straddling two monitors is still on-screen."""
    info = WindowInfo(
        hwnd=123,
        pid=1,
        exe_name="x.exe",
        title="X",
        bounds=Rect(1900, 100, 200, 200),  # 20px on primary, 180px on secondary
        visible=True,
        minimized=False,
    )
    monitors = [Rect(0, 0, 1920, 1080), Rect(1920, 0, 1920, 1080)]
    _stub_spatial(monkeypatch, info=info, monitors=monitors)
    out = tokens_mod.inspect(_token())
    assert out["off_screen"] is False


def test_inspect_minimized_window_is_also_off_screen(monkeypatch):
    """Minimized windows live at -32000 and don't intersect any monitor.
    Both flags are independent, so both should be True."""
    info = WindowInfo(
        hwnd=123,
        pid=1,
        exe_name="x.exe",
        title="X",
        bounds=Rect(-32000, -32000, 160, 28),
        visible=True,
        minimized=True,
    )
    _stub_spatial(monkeypatch, info=info, monitors=[Rect(0, 0, 1920, 1080)])
    out = tokens_mod.inspect(_token())
    assert out["minimized"] is True
    assert out["off_screen"] is True
