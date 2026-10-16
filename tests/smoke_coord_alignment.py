"""Smoke test: verify PrintWindow image origin matches GetWindowRect.

For OCR translation to land on the right pixels, the (0, 0) of the captured
image MUST correspond to GetWindowRect(left, top). If they disagree by even
the height of a title bar, every OCR-driven click misses.

Run from the repo root: python tests/smoke_coord_alignment.py
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> int:
    # Set DPI awareness BEFORE anything else (matches server.py startup).
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except Exception:
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except Exception:
            ctypes.windll.user32.SetProcessDPIAware()

    import win32gui

    from iris import spatial as spatial_mod
    from iris import vision as vision_mod

    # Open notepad as a clean test subject (system app, predictable rect).
    # Modern Notepad on Win11 is a Store app that runs as a child of a launcher;
    # the launcher exits quickly and the real PID isn't proc.pid. Search by
    # exe name across all running windows for up to 3 seconds.
    proc = subprocess.Popen(["notepad.exe"])
    hwnd = None
    deadline = time.time() + 3.0
    while time.time() < deadline and hwnd is None:
        for w in spatial_mod.enumerate_windows():
            if w.exe_name.lower() == "notepad.exe" and w.title:
                hwnd = w.hwnd
                break
        if hwnd is None:
            time.sleep(0.15)
    if hwnd is None:
        try:
            proc.kill()
        except Exception:
            pass
        print("FAIL: could not locate notepad window after 3s")
        return 1

    try:
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        rect_w = r - l
        rect_h = b - t
        img = vision_mod.capture_window(hwnd)
        img_w, img_h = img.size

        print(f"GetWindowRect: ({l}, {t}) -> ({r}, {b})  size {rect_w}x{rect_h}")
        print(f"PrintWindow image size: {img_w}x{img_h}")

        # The capture should match the window rect EXACTLY. Even 1-pixel mismatch
        # indicates a DPI awareness bug or a client-vs-window-rect confusion.
        if (img_w, img_h) != (rect_w, rect_h):
            print(f"FAIL: image size {img_w}x{img_h} != window rect {rect_w}x{rect_h}")
            print("Likely cause: process is not per-monitor DPI aware, or PrintWindow")
            print("is using client coords. Either breaks OCR coord translation.")
            return 1

        # Also check current_bounds agrees with GetWindowRect (which it should
        # since they read the same API).
        live = spatial_mod.current_bounds(hwnd)
        if live is None:
            print("FAIL: current_bounds returned None for a live hwnd")
            return 1
        if (live.x, live.y, live.width, live.height) != (l, t, rect_w, rect_h):
            print(f"FAIL: current_bounds {live} != GetWindowRect ({l},{t},{rect_w},{rect_h})")
            return 1

        print("PASS: PrintWindow image origin = GetWindowRect.topleft")
        print("PASS: spatial.current_bounds agrees with GetWindowRect")
        return 0
    finally:
        try:
            proc.terminate()
            proc.wait(timeout=2)
        except Exception:
            proc.kill()


if __name__ == "__main__":
    sys.exit(main())
