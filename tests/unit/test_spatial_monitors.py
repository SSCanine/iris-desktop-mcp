from iris.geometry import Rect
from iris.spatial import get_monitor_for_window

MONITORS = [
    Rect(0, 0, 1920, 1080),  # primary, monitor 0
    Rect(1920, 0, 2560, 1440),  # secondary right, monitor 1
    Rect(-1920, 0, 1920, 1080),  # tertiary left, monitor 2
]


def test_window_fully_on_primary():
    w = Rect(100, 100, 800, 600)
    assert get_monitor_for_window(w, monitors=MONITORS) == 0


def test_window_on_secondary():
    w = Rect(2000, 100, 800, 600)
    assert get_monitor_for_window(w, monitors=MONITORS) == 1


def test_window_on_tertiary():
    w = Rect(-1500, 100, 800, 600)
    assert get_monitor_for_window(w, monitors=MONITORS) == 2


def test_window_straddling_primary_and_secondary_largest_area_wins():
    # 80% on monitor 1, 20% on monitor 0
    w = Rect(1800, 100, 800, 600)
    assert get_monitor_for_window(w, monitors=MONITORS) == 1


def test_window_off_screen_returns_negative():
    w = Rect(10000, 10000, 100, 100)
    assert get_monitor_for_window(w, monitors=MONITORS) == -1
