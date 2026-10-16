"""01: Hello Iris.

The minimum viable Iris program. Confirms the install works, prints the
monitor layout, and verifies which backends are available. Run this first
after installing iris-mcp.

    python examples/01_hello_iris.py
"""

from __future__ import annotations

import ctypes

# DPI awareness must come before any Win32 import (matches what server.py does).
try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass

from iris import semantic, spatial, vision
from iris._version import __version__


def main() -> None:
    print(f"Iris {__version__}")
    print()

    # Backend availability
    print("Backends")
    print(f"  Win32 (spatial)    : {'OK' if spatial.HAS_WIN32 else 'MISSING'}")
    print(f"  UIA (semantic)     : {'OK' if semantic.HAS_UIA else 'MISSING'}")
    print(f"  Tesseract (vision) : {'OK' if vision._TESSERACT_OK else 'MISSING'}")
    print()

    # Monitor topology - what Iris actually sees
    print("Monitors")
    for i, m in enumerate(spatial.list_monitors()):
        marker = "primary" if i == 0 else f"monitor {i}"
        print(
            f"  {marker:<10} ({m.x:>6}, {m.y:>6}) -> ({m.right:>6}, {m.bottom:>6})  "
            f"size {m.width}x{m.height}"
        )
    print()

    # Active windows (top 5)
    print("Active windows (top 5)")
    windows = spatial.enumerate_windows()
    for w in windows[:5]:
        print(f"  hwnd={w.hwnd:<10} pid={w.pid:<6} {w.exe_name:<24} {w.title[:60]}")


if __name__ == "__main__":
    main()
