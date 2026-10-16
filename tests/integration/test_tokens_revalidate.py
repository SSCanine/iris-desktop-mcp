"""Integration tests for token revalidation against the harness."""

import time

import pytest

from iris.spatial import HAS_WIN32, _make_window_info, get_monitor_for_window
from iris.tokens import FocusToken, inspect, revalidate

pytestmark = pytest.mark.skipif(not HAS_WIN32, reason="Win32 only")


def _make_token(hwnd):
    info = _make_window_info(hwnd)
    monitor = get_monitor_for_window(info.bounds)
    return FocusToken.create(
        hwnd=info.hwnd,
        pid=info.pid,
        exe_name=info.exe_name,
        title=info.title,
        monitor_index=max(monitor, 0),
        bounds=info.bounds,
    )


def test_revalidate_returns_true_for_live_window(iris_harness):
    token = _make_token(iris_harness.hwnd)
    assert revalidate(token) is True


def test_revalidate_caches_result_for_250ms(iris_harness):
    token = _make_token(iris_harness.hwnd)
    revalidate(token)
    first = token.last_revalidated_at
    revalidate(token)
    # Second call within TTL should not update timestamp
    assert token.last_revalidated_at == first


def test_revalidate_returns_false_when_window_killed(iris_harness):
    token = _make_token(iris_harness.hwnd)
    assert revalidate(token) is True
    iris_harness.terminate()
    time.sleep(0.5)
    # Force past the TTL
    token.last_revalidated_at = 0.0
    # Process is dead, no other window with same pid OR same exe+title exists
    # (other python.exe processes don't have IRIS_TEST_HARNESS title)
    assert revalidate(token) is False


def test_inspect_returns_full_report(iris_harness):
    token = _make_token(iris_harness.hwnd)
    report = inspect(token)
    assert report["alive"] is True
    assert "bounds" in report
    assert "monitor" in report
    assert "occluded" in report
    assert "popups" in report
    assert isinstance(report["popups"], list)
