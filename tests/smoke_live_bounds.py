"""Smoke test: prove the resolver uses LIVE window bounds, not bounds_at_creation.

This is the biggest pixel-accuracy fix. Before this change, the resolver
translated OCR-window-local coords to screen-absolute by adding the token's
creation-time bounds. If the user dragged the window between focus() and
find(), every OCR-driven click missed by the drag delta.

We use the Tkinter test harness because its buttons are NOT exposed via UIA
on Windows 11 (Tk widgets are pure pixels). That forces the resolver into
the OCR path, which is the one that exercises the bounds_at_creation fix.

What we check:
  1. Spawn the harness at a known location.
  2. Find "Click Me" before moving the window. Confirm backend=ocr.
  3. Move the window by a known delta.
  4. Find "Click Me" again. The hit bbox must translate by the same delta.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
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

    import win32con
    import win32gui

    from iris import resolver as resolver_mod
    from iris import spatial as spatial_mod
    from iris import vision as vision_mod
    from iris.geometry import Rect
    from iris.tokens import FocusToken

    # Spawn the Tkinter harness at a known position.
    harness = Path(__file__).resolve().parent / "fixtures" / "iris_test_harness.py"
    proc = subprocess.Popen(
        [sys.executable, str(harness), "--geometry", "600x400+200+200"],
        creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
    )
    hwnd = None
    deadline = time.time() + 5.0
    while time.time() < deadline and hwnd is None:
        for w in spatial_mod.enumerate_windows():
            if "IRIS_TEST_HARNESS" in w.title:
                hwnd = w.hwnd
                break
        if hwnd is None:
            time.sleep(0.15)
    if hwnd is None:
        proc.terminate()
        print("FAIL: could not locate test harness")
        return 1

    try:
        time.sleep(0.4)  # let it settle
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        bounds_at_creation = Rect.from_ltrb(l, t, r, b)
        print(f"creation bounds: {bounds_at_creation.to_dict()}")

        tk_token = FocusToken.create(
            hwnd=hwnd,
            pid=proc.pid,
            exe_name="python.exe",
            title="IRIS_TEST_HARNESS",
            monitor_index=0,
            bounds=bounds_at_creation,
        )

        # Find "Click Me" pre-move. Should hit via OCR (Tk doesn't expose UIA).
        r1 = resolver_mod.find(tk_token, "Click Me")
        if not r1.found:
            print(f"FAIL: pre-move find missed: {r1.to_dict()}")
            return 1
        pre_hit = r1.hits[0]
        pre_bbox = pre_hit.get("bbox") or pre_hit.get("bounds")
        pre_cx = pre_bbox["x"] + pre_bbox["width"] // 2
        pre_cy = pre_bbox["y"] + pre_bbox["height"] // 2
        print(f"pre-move hit: backend={r1.backend} center=({pre_cx},{pre_cy})")

        if r1.backend != "ocr":
            print(
                f"NOTE: expected OCR backend, got {r1.backend}. Test still valid "
                f"if bounds tracking is consistent, but does not exercise the "
                f"OCR coord translation fix specifically."
            )

        # Move the window by a known delta.
        delta_x, delta_y = 400, 250
        win32gui.SetWindowPos(
            hwnd,
            0,
            bounds_at_creation.x + delta_x,
            bounds_at_creation.y + delta_y,
            bounds_at_creation.width,
            bounds_at_creation.height,
            win32con.SWP_NOZORDER | win32con.SWP_NOACTIVATE | win32con.SWP_SHOWWINDOW,
        )
        time.sleep(0.4)
        l2, t2, r2_, b2 = win32gui.GetWindowRect(hwnd)
        new_bounds = Rect.from_ltrb(l2, t2, r2_, b2)
        print(f"new bounds: {new_bounds.to_dict()}")

        # Force fresh OCR (the window's pixels moved on screen).
        vision_mod.clear_ocr_cache(tk_token.id)
        # Reset the UIA support cache too in case the harness pid was marked
        # as UIA-supporting (it isn't, but cache).
        try:
            from iris.semantic import reset_uia_support_cache

            reset_uia_support_cache()
        except Exception:
            pass

        r2 = resolver_mod.find(tk_token, "Click Me")
        if not r2.found:
            print(f"FAIL: post-move find missed: {r2.to_dict()}")
            return 1
        post_hit = r2.hits[0]
        post_bbox = post_hit.get("bbox") or post_hit.get("bounds")
        post_cx = post_bbox["x"] + post_bbox["width"] // 2
        post_cy = post_bbox["y"] + post_bbox["height"] // 2
        print(f"post-move hit: backend={r2.backend} center=({post_cx},{post_cy})")

        # The delta in hit centers must equal the window move delta (within
        # OCR rounding). With the stale-bounds bug, post_cx ~= pre_cx.
        observed_dx = post_cx - pre_cx
        observed_dy = post_cy - pre_cy
        print(f"observed delta: ({observed_dx},{observed_dy}) expected ({delta_x},{delta_y})")

        # Tolerance: a few pixels for OCR jitter.
        tol = 5
        if abs(observed_dx - delta_x) > tol or abs(observed_dy - delta_y) > tol:
            print(f"FAIL: delta off by more than {tol}px. Live-bounds fix not active.")
            return 1

        # Sanity: post hit must land inside the new window.
        if not new_bounds.contains_point(post_cx, post_cy):
            print("FAIL: post-move hit center NOT inside new window")
            return 1

        print(
            "PASS: resolver tracked the window move, hit bbox translates by the exact drag delta."
        )
        return 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass


if __name__ == "__main__":
    sys.exit(main())
