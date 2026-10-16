"""Monitor topology is discovered, ordered primary-first, and shared by every
consumer (screen_info, see/screenshot(monitor=), tokens). No machine-specific
assumptions anywhere."""

from __future__ import annotations

import pytest

from iris import spatial, vision
from iris.geometry import Rect
from iris.spatial import MonitorInfo


def _fake_topology():
    # Deliberately NOT primary-first, and with a display above the primary
    # (negative y) like a real stacked setup.
    return [
        MonitorInfo(0, Rect(-3840, -1080, 3840, 2160), Rect(-3840, -1080, 3840, 2088), 144, 1.5, False, r"\\.\DISPLAY2"),
        MonitorInfo(1, Rect(0, 0, 2560, 1080), Rect(0, 0, 2560, 1032), 96, 1.0, True, r"\\.\DISPLAY3"),
        MonitorInfo(2, Rect(2560, 0, 1920, 1080), Rect(2560, 0, 1920, 1032), 96, 1.0, False, r"\\.\DISPLAY1"),
        MonitorInfo(3, Rect(0, -2160, 3840, 2160), Rect(0, -2160, 3840, 2076), 168, 1.75, False, r"\\.\DISPLAY4"),
    ]  # fmt: skip


def _sorted_like_enumerator(infos):
    raw = sorted(infos, key=lambda m: (not m.primary, m.bounds.y, m.bounds.x))
    return [
        MonitorInfo(i, m.bounds, m.work_area, m.dpi, m.scale, m.primary, m.device)
        for i, m in enumerate(raw)
    ]


@pytest.fixture
def topology(monkeypatch):
    infos = _sorted_like_enumerator(_fake_topology())
    monkeypatch.setattr(spatial, "_enumerate_monitors_win32", lambda: infos)
    monkeypatch.setattr(spatial, "HAS_WIN32", True)
    monkeypatch.setattr(spatial, "_topology_signature", lambda: (4, -3840, -2160, 8320, 3240))
    monkeypatch.setattr(spatial, "virtual_desktop", lambda: Rect(-3840, -2160, 8320, 3240))
    spatial.list_monitor_info(force_refresh=True)
    return infos


def test_primary_first_then_top_to_bottom_left_to_right(topology):
    infos = spatial.list_monitor_info()
    assert [m.primary for m in infos] == [True, False, False, False]
    assert infos[0].bounds == Rect(0, 0, 2560, 1080)
    # After the primary: y ascending (-2160 above, then -1080), then x.
    assert [m.bounds.y for m in infos[1:]] == [-2160, -1080, 0]
    assert [m.index for m in infos] == [0, 1, 2, 3]
    assert [m.capture_id for m in infos] == [1, 2, 3, 4]
    assert spatial.list_monitors() == [m.bounds for m in infos]
    assert spatial.primary_monitor().device.endswith("DISPLAY3")


def test_monitor_info_dict_carries_scale_and_ids(topology):
    d = spatial.list_monitor_info()[0].to_dict()
    for key in (
        "x",
        "y",
        "width",
        "height",
        "index",
        "capture_id",
        "work_area",
        "dpi",
        "scale",
        "primary",
        "device",
    ):
        assert key in d
    assert d["scale"] == 1.0 and d["primary"] is True and d["capture_id"] == 1
    top = [m for m in spatial.list_monitor_info() if m.bounds.y == -2160][0]
    assert top.scale == 1.75


def test_capture_id_resolution_and_bounds(topology):
    assert spatial.monitor_for_capture_id(1).primary
    assert spatial.monitor_for_capture_id(4).bounds == Rect(2560, 0, 1920, 1080)
    with pytest.raises(ValueError) as e:
        spatial.monitor_for_capture_id(5)
    assert "4 monitor(s)" in str(e.value)
    with pytest.raises(ValueError):
        spatial.monitor_for_capture_id(0)


def test_capture_bounds_maps_monitor_argument(topology):
    assert vision.capture_bounds(monitor=0) == Rect(-3840, -2160, 8320, 3240)
    assert vision.capture_bounds(monitor=1) == Rect(0, 0, 2560, 1080)
    assert vision.capture_bounds(monitor=2) == Rect(0, -2160, 3840, 2160)
    assert vision.capture_bounds(bounds=Rect(5, 6, 7, 8), monitor=3) == Rect(5, 6, 7, 8)
    assert vision.capture_origin(monitor=2) == (0, -2160)


def test_capture_grabs_the_resolved_rect(topology, monkeypatch):
    grabbed = []

    class FakeRaw:
        def __init__(self, area):
            self.width, self.height = area["width"], area["height"]
            self.rgb = bytes(self.width * self.height * 3)

    class FakeSct:
        def grab(self, area):
            grabbed.append(area)
            return FakeRaw({"width": 4, "height": 3})

    monkeypatch.setattr(vision, "_get_sct", lambda: FakeSct())
    img = vision.capture(monitor=3)
    assert img.size == (4, 3)
    assert grabbed[-1] == {"top": -1080, "left": -3840, "width": 3840, "height": 2160}


def test_cache_invalidates_when_topology_changes(monkeypatch):
    calls = []
    infos3 = _sorted_like_enumerator(_fake_topology()[:3])
    infos4 = _sorted_like_enumerator(_fake_topology())
    state = {"sig": (3, 0, 0, 1, 1), "infos": infos3}
    monkeypatch.setattr(spatial, "HAS_WIN32", True)
    monkeypatch.setattr(spatial, "_topology_signature", lambda: state["sig"])
    monkeypatch.setattr(
        spatial, "_enumerate_monitors_win32", lambda: calls.append(1) or state["infos"]
    )
    assert len(spatial.list_monitor_info(force_refresh=True)) == 3
    assert len(spatial.list_monitor_info()) == 3 and len(calls) == 1  # cached
    state["sig"] = (4, 0, 0, 2, 2)  # a monitor was plugged in
    state["infos"] = infos4
    assert len(spatial.list_monitor_info()) == 4 and len(calls) == 2  # refreshed at once


def test_get_monitor_for_window_uses_same_ordering(topology):
    # A window on the 175% display above the primary.
    idx = spatial.get_monitor_for_window(Rect(100, -2000, 800, 600))
    assert spatial.list_monitor_info()[idx].scale == 1.75
    assert idx == 1  # primary is 0, the top display sorts next (y = -2160)
