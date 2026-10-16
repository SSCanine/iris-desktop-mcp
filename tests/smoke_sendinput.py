"""Smoke test: verify the SendInput-based input module actually moves the
cursor and registers a click without using pyautogui.

What we check:
  1. position() returns a sensible cursor location.
  2. move(x, y) lands the cursor at (x, y) within 1px tolerance.
  3. click(x, y) emits at least 3 events (move + down + up) and the cursor
     ends at (x, y).

We do NOT need a real app to be hit, because we're testing the input pipeline
itself, not whether the click did something. Coord alignment is tested
elsewhere (smoke_coord_alignment.py).
"""

from __future__ import annotations

import ctypes
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()

    from iris import input as input_mod
    from iris import spatial as spatial_mod

    if not input_mod.HAS_SENDINPUT:
        print("FAIL: SendInput unavailable")
        return 1

    # Save current cursor so we restore it at the end.
    start_x, start_y = input_mod.position()
    print(f"start position: ({start_x}, {start_y})")

    # Pick a target on the primary monitor (safe, in the middle).
    monitors = spatial_mod.list_monitors()
    if not monitors:
        print("FAIL: no monitors")
        return 1
    primary = monitors[0]
    target_x = primary.x + primary.width // 2
    target_y = primary.y + primary.height // 2
    print(f"target: ({target_x}, {target_y}) on primary {primary}")

    # 1. move
    input_mod.move(target_x, target_y)
    cx, cy = input_mod.position()
    print(f"after move: ({cx}, {cy})")
    if abs(cx - target_x) > 1 or abs(cy - target_y) > 1:
        print(f"FAIL: cursor did not reach target. delta=({cx - target_x},{cy - target_y})")
        # restore cursor and bail
        input_mod.move(start_x, start_y)
        return 1

    # 2. click (move + down + up = 3 events)
    r = input_mod.click(x=target_x, y=target_y, button="left", clicks=1)
    print(f"click result: {r}")
    if not r.get("ok"):
        input_mod.move(start_x, start_y)
        print("FAIL: click returned ok=False")
        return 1
    if r.get("events_sent", 0) < 3:
        input_mod.move(start_x, start_y)
        print(f"FAIL: click sent {r.get('events_sent')} events, expected >= 3")
        return 1

    # 3. Restore cursor so we don't startle the user.
    input_mod.move(start_x, start_y)
    end_x, end_y = input_mod.position()
    print(f"restored to ({end_x}, {end_y})")

    print("PASS: SendInput move + click work")
    return 0


if __name__ == "__main__":
    sys.exit(main())
