"""Bench scenarios: predefined ways of stressing the click pipeline.

Each scenario is a generator of (name, setup_fn, target_ids) describing what
the harness window should look like, and which targets to drive against in
that state. The runner calls setup_fn between target attempts so each scenario
can mutate the window (move, resize, occlude, etc.).

Setups are passed the live hwnd and the bench context. They return either
None (success) or a string explaining why they couldn't run (then runner
skips that scenario).
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field

from iris import spatial as spatial_mod

# Setup signature: (hwnd, ctx) -> Optional[str]
SetupFn = Callable[[int, dict], str | None]


@dataclass
class Scenario:
    id: str
    description: str
    setup: SetupFn
    target_ids: list[str] = field(default_factory=list)
    # If True, scenario should run once per monitor. Runner handles fan-out.
    per_monitor: bool = False


# ---------------------------------------------------------------------------
# Window manipulation helpers (no pyautogui, direct Win32)
# ---------------------------------------------------------------------------
def _move(hwnd: int, x: int, y: int, w: int | None = None, h: int | None = None) -> None:
    """Move/resize a window. Width/height defaulted to current if not given."""
    import win32con
    import win32gui

    if w is None or h is None:
        cur = spatial_mod.current_bounds(hwnd)
        if cur is None:
            return
        w = w if w is not None else cur.width
        h = h if h is not None else cur.height
    win32gui.SetWindowPos(
        hwnd,
        0,
        int(x),
        int(y),
        int(w),
        int(h),
        win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
    )
    time.sleep(0.25)


def _resize(hwnd: int, w: int, h: int) -> None:
    cur = spatial_mod.current_bounds(hwnd)
    if cur is None:
        return
    _move(hwnd, cur.x, cur.y, w, h)


# ---------------------------------------------------------------------------
# Setups
# ---------------------------------------------------------------------------
def _setup_static(hwnd: int, ctx: dict) -> str | None:
    """No-op. Window stays where it was placed."""
    return None


def _setup_drag(hwnd: int, ctx: dict) -> str | None:
    """Move the window 400px right + 250px down so old bounds are obviously stale."""
    cur = spatial_mod.current_bounds(hwnd)
    if cur is None:
        return "harness window vanished before drag"
    _move(hwnd, cur.x + 400, cur.y + 250)
    return None


def _setup_resize(hwnd: int, ctx: dict) -> str | None:
    """Shrink the window to 700x500. Forces button layout to reflow."""
    cur = spatial_mod.current_bounds(hwnd)
    if cur is None:
        return "harness window vanished before resize"
    _move(hwnd, cur.x, cur.y, 700, 500)
    return None


def _setup_park_each_monitor(hwnd: int, ctx: dict) -> str | None:
    """Park the harness on the monitor identified by ctx['monitor_index']."""
    idx = ctx.get("monitor_index", 0)
    monitors = spatial_mod.list_monitors()
    if idx < 0 or idx >= len(monitors):
        return f"no monitor at index {idx} (have {len(monitors)})"
    m = monitors[idx]
    # Park 80px in from the monitor's top-left.
    cur = spatial_mod.current_bounds(hwnd)
    if cur is None:
        return "harness window vanished"
    _move(hwnd, m.x + 80, m.y + 80, cur.width, cur.height)
    return None


def _setup_occluded_then_raise(hwnd: int, ctx: dict) -> str | None:
    """Spawn a covering window and let bring_to_front rescue our harness.

    This exercises occlusion-retry + the printwindow capture path.
    """
    # We don't actually spawn an occluder here; we rely on the user / other
    # apps to potentially overlap. The bring_to_front call is the real test.
    try:
        spatial_mod.bring_to_front(hwnd)
    except Exception as e:
        return f"bring_to_front raised: {e}"
    time.sleep(0.2)
    return None


# ---------------------------------------------------------------------------
# Scenario library
# ---------------------------------------------------------------------------
ALL_TARGET_IDS = [
    "medium_center",
    "tiny_btn",
    "short_label",
    "wide_row",
    "icon_label",
    "huge_btn",
    "edge_right",
    "ambiguous_a",
    "ambiguous_b",
    "lowercase_only",
]

# Targets that mainly stress the bounds/coord path. We exclude the ambiguous
# pair AND short_label so the accuracy signal from window mutations isn't
# drowned in OCR-on-2-letters jitter.
COORD_TARGETS = [
    "medium_center",
    "tiny_btn",
    "wide_row",
    "icon_label",
    "huge_btn",
    "edge_right",
    "lowercase_only",
]


SCENARIOS: list[Scenario] = [
    Scenario(
        id="baseline_static",
        description="Window placed once, no mutation. The cleanest possible pass.",
        setup=_setup_static,
        target_ids=ALL_TARGET_IDS,
    ),
    Scenario(
        id="window_dragged",
        description="Window moved 400px right + 250px down before each click. Verifies live-bounds fix.",
        setup=_setup_drag,
        target_ids=COORD_TARGETS,
    ),
    Scenario(
        id="window_resized",
        description="Window resized to 700x500 (button layout reflows). Tests bounds-tracking under reshape.",
        setup=_setup_resize,
        target_ids=COORD_TARGETS,
    ),
    Scenario(
        id="per_monitor",
        description="Window parked on each physical monitor in turn. Tests DPI-aware coord delivery across 100/125/150% scales.",
        setup=_setup_park_each_monitor,
        target_ids=COORD_TARGETS,
        per_monitor=True,
    ),
    Scenario(
        id="raise_then_click",
        description="bring_to_front is called before each click. Tests that focus-rescue + capture line up.",
        setup=_setup_occluded_then_raise,
        target_ids=COORD_TARGETS,
    ),
]


def scenario_by_id(scenario_id: str) -> Scenario | None:
    for s in SCENARIOS:
        if s.id == scenario_id:
            return s
    return None
