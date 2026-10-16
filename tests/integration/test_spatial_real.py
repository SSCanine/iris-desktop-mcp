import pytest

from iris.spatial import (
    HAS_WIN32,
    enumerate_windows,
    get_foreground_window_info,
    get_monitor_for_window,
    list_monitors,
)

pytestmark = pytest.mark.skipif(not HAS_WIN32, reason="Win32 only")


def test_enumerate_windows_returns_list():
    wins = enumerate_windows()
    assert isinstance(wins, list)
    assert len(wins) > 0


def test_enumerate_windows_have_titles():
    wins = enumerate_windows(titled_only=True)
    for w in wins:
        assert w.title != ""


def test_window_info_serializes():
    wins = enumerate_windows()
    for w in wins[:5]:
        d = w.to_dict()
        assert "hwnd" in d and "title" in d and "bounds" in d


def test_list_monitors_returns_at_least_one():
    monitors = list_monitors()
    assert len(monitors) >= 1
    for m in monitors:
        assert m.width > 0
        assert m.height > 0


def test_foreground_window_has_valid_hwnd():
    info = get_foreground_window_info()
    if info is None:
        pytest.skip("No foreground window")
    assert info.hwnd > 0
    assert info.bounds.width >= 0


def test_foreground_window_maps_to_a_monitor():
    info = get_foreground_window_info()
    if info is None or info.minimized:
        pytest.skip("No usable foreground window")
    idx = get_monitor_for_window(info.bounds)
    assert idx >= 0
