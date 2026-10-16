"""Tests for spatial.wait_for_window_visible — the post-restore poll helper."""

import pytest

from iris import spatial as spatial_mod
from iris.geometry import Rect
from iris.spatial import WindowInfo, wait_for_window_visible


def _info(hwnd=123, minimized=False, x=100):
    return WindowInfo(
        hwnd=hwnd,
        pid=1,
        exe_name="x.exe",
        title="X",
        bounds=Rect(x, 0, 100, 100),
        visible=True,
        minimized=minimized,
    )


@pytest.fixture
def patch_iswindow_true(monkeypatch):
    if not spatial_mod.HAS_WIN32:
        pytest.skip("Win32 not available")
    import win32gui

    monkeypatch.setattr(win32gui, "IsWindow", lambda h: True)


def test_returns_immediately_when_already_visible(monkeypatch, patch_iswindow_true):
    visible = _info(minimized=False, x=200)
    monkeypatch.setattr(spatial_mod, "_make_window_info", lambda h: visible)
    out = wait_for_window_visible(123, timeout_ms=500, poll_ms=10)
    assert out is visible


def test_polls_until_window_materializes(monkeypatch, patch_iswindow_true):
    """Stale (minimized at -32000) for the first 2 calls, then fresh."""
    minimized = _info(minimized=True, x=-32000)
    materialized = _info(minimized=False, x=200)
    state = {"calls": 0}

    def fake_make(hwnd):
        state["calls"] += 1
        return minimized if state["calls"] <= 2 else materialized

    monkeypatch.setattr(spatial_mod, "_make_window_info", fake_make)
    out = wait_for_window_visible(123, timeout_ms=1000, poll_ms=10)
    assert out is materialized
    assert state["calls"] >= 3


def test_returns_latest_snapshot_on_timeout(monkeypatch, patch_iswindow_true):
    """If window never recovers, return the most recent (still-stale) snapshot."""
    stale = _info(minimized=True, x=-32000)
    monkeypatch.setattr(spatial_mod, "_make_window_info", lambda h: stale)
    out = wait_for_window_visible(123, timeout_ms=50, poll_ms=10)
    assert out is stale  # latest, even though conditions never satisfied


def test_returns_none_when_hwnd_invalid(monkeypatch):
    if not spatial_mod.HAS_WIN32:
        pytest.skip("Win32 not available")
    import win32gui

    monkeypatch.setattr(win32gui, "IsWindow", lambda h: False)
    out = wait_for_window_visible(999, timeout_ms=50)
    assert out is None


def test_returns_last_when_make_info_starts_returning_none(monkeypatch, patch_iswindow_true):
    """If _make_window_info eventually starts returning None (window died mid-poll),
    return the last good snapshot."""
    last_good = _info(minimized=True, x=-32000)
    state = {"calls": 0}

    def fake_make(hwnd):
        state["calls"] += 1
        if state["calls"] == 1:
            return last_good
        return None

    monkeypatch.setattr(spatial_mod, "_make_window_info", fake_make)
    out = wait_for_window_visible(123, timeout_ms=200, poll_ms=10)
    assert out is last_good
