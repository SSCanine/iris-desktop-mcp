"""04: Find and click an element in a real app.

Spawns Notepad, focuses it, finds the File menu via UIA, and clicks it via
the UIA Invoke fast path (no mouse motion). The same code works against any
app that exposes UIA. For apps that don't (Tk, custom Qt), the resolver
silently falls back to OCR + widget upgrade.

    python examples/04_find_and_click_real_app.py
"""

from __future__ import annotations

import ctypes
import subprocess
import time

try:
    ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
except Exception:
    pass

from iris import resolver, semantic, spatial
from iris.tokens import FocusToken


def spawn_notepad() -> int | None:
    """Open notepad and return its hwnd once it appears."""
    subprocess.Popen(["notepad.exe"])
    deadline = time.time() + 5.0
    while time.time() < deadline:
        for w in spatial.enumerate_windows():
            if w.exe_name.lower() == "notepad.exe" and w.title:
                return w.hwnd
        time.sleep(0.1)
    return None


def main() -> None:
    hwnd = spawn_notepad()
    if hwnd is None:
        print("Could not locate notepad after 5s.")
        return
    print(f"Notepad hwnd: {hwnd}")

    bounds = spatial.current_bounds(hwnd)
    if bounds is None:
        print("Notepad bounds vanished.")
        return
    tok = FocusToken.create(
        hwnd=hwnd,
        pid=0,
        exe_name="notepad.exe",
        title="Notepad",
        monitor_index=spatial.get_monitor_for_window(bounds),
        bounds=bounds,
    )
    spatial.bring_to_front(hwnd)

    # Look for the File menu. Modern Notepad on Win11 uses different labels;
    # we try a few candidates so this example works on Win10 + Win11.
    for candidate in ("File", "Edit", "View", "Settings"):
        result = resolver.find(tok, candidate)
        if result.found:
            top = result.hits[0]
            print(
                f"Found {candidate!r} via {result.backend} "
                f"in {result.elapsed_ms:.0f}ms. "
                f"upgraded_to_uia={top.get('upgraded_to_uia', False)}"
            )
            # If the hit is a UIA control we trust, invoke without mouse motion.
            ctrl = result.controls[0] if result.controls else None
            if ctrl is not None and semantic.is_invoke_trusted(ctrl):
                inv = semantic.try_pattern_click(ctrl)
                print(f"UIA Invoke: {inv}")
            else:
                print("(UIA Invoke not trusted for this control; would mouse-click instead)")
            break
    else:
        print("No menu items located. Notepad may have a non-standard UI.")
        print("Try iris-mcp-doctor to confirm UIA backend is available.")


if __name__ == "__main__":
    main()
