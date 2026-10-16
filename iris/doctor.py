"""`iris-mcp doctor`: environment diagnostics.

Prints a readable report covering:
  * Python + OS version
  * DPI awareness mode (after we set it)
  * Monitor topology (count, sizes, scales)
  * UIA + win32 + clipboard availability
  * Tesseract OCR location and version
  * Active MCP package version

Exits 0 if all critical components are available, 1 otherwise.

Designed to be the first thing a new user runs after install ("does this
work on my machine"), and the first thing we ask for in bug reports.
"""

from __future__ import annotations

import ctypes
import os
import platform
import sys


# Set DPI awareness FIRST so the monitor topology we print is in physical
# pixels, not virtualized. Matches what the MCP server does at startup.
def _set_dpi_awareness() -> str:
    try:
        ctx = ctypes.c_void_p(-4)
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctx):
            return "per_monitor_v2"
    except (AttributeError, OSError):
        pass
    try:
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return "per_monitor_v1"
    except (AttributeError, OSError):
        pass
    try:
        if ctypes.windll.user32.SetProcessDPIAware():
            return "system_aware"
    except (AttributeError, OSError):
        pass
    return "unaware"


GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
GRAY = "\033[90m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _ok(text: str) -> str:
    return f"{GREEN}{text}{RESET}"


def _warn(text: str) -> str:
    return f"{YELLOW}{text}{RESET}"


def _err(text: str) -> str:
    return f"{RED}{text}{RESET}"


def _gray(text: str) -> str:
    return f"{GRAY}{text}{RESET}"


def main() -> int:
    dpi_mode = _set_dpi_awareness() if sys.platform == "win32" else "n/a (not Windows)"

    print(f"{BOLD}Iris doctor{RESET}")

    # Version
    try:
        from iris._version import __version__
    except Exception as e:
        __version__ = f"unknown ({e})"
    print(f"  iris-mcp version       : {__version__}")
    print(f"  python                 : {platform.python_version()} ({sys.executable})")
    print(
        f"  platform               : {platform.system()} {platform.release()} ({platform.machine()})"
    )

    critical_ok = True

    if sys.platform != "win32":
        print(_err("  ! Iris is Windows-only. Most features will not work on this platform."))
        critical_ok = False

    # DPI mode
    label_ok = dpi_mode in ("per_monitor_v2", "per_monitor_v1")
    print(
        f"  DPI mode               : "
        f"{_ok(dpi_mode) if label_ok else _warn(dpi_mode)}"
        f"  {_gray('(per_monitor_v2 is best)')}"
    )
    if dpi_mode == "unaware":
        critical_ok = False

    # win32 + UIA + clipboard
    try:
        from iris import spatial as spatial_mod

        print(
            f"  Win32 (pywin32)        : "
            f"{_ok('available') if spatial_mod.HAS_WIN32 else _err('MISSING')}"
        )
        if not spatial_mod.HAS_WIN32:
            critical_ok = False
    except Exception as e:
        print(f"  Win32 (pywin32)        : {_err(f'import failed: {e}')}")
        critical_ok = False

    try:
        from iris import semantic as semantic_mod

        print(
            f"  UIA (uiautomation)     : "
            f"{_ok('available') if semantic_mod.HAS_UIA else _warn('not installed')}"
            f"  {_gray('(install for accessibility-tree queries)')}"
        )
    except Exception as e:
        print(f"  UIA (uiautomation)     : {_warn(f'import failed: {e}')}")

    try:
        from iris import system as system_mod

        print(
            f"  Clipboard (win32clip.) : "
            f"{_ok('available') if system_mod.HAS_CLIPBOARD else _warn('not installed')}"
        )
        print(
            f"  psutil (processes)     : "
            f"{_ok('available') if system_mod.HAS_PSUTIL else _warn('not installed')}"
        )
        print(
            f"  winreg (registry)      : "
            f"{_ok('available') if system_mod.HAS_WINREG else _warn('not Windows')}"
        )
    except Exception as e:
        print(f"  system module          : {_warn(f'import failed: {e}')}")

    # Tesseract
    try:
        from iris import vision as vision_mod
        from iris.tesseract_bootstrap import locate_tesseract

        tess = locate_tesseract()
        if tess:
            print(f"  Tesseract OCR          : {_ok(str(tess))}")
        else:
            print(
                f"  Tesseract OCR          : "
                f"{_warn('not found')}  "
                f"{_gray('OCR fallback disabled. Install via winget install UB-Mannheim.TesseractOCR')}"
            )
        if not vision_mod._TESSERACT_OK:
            print(f"  Tesseract Python bind  : {_warn('pytesseract import failed')}")
    except Exception as e:
        print(f"  Tesseract OCR          : {_warn(f'check failed: {e}')}")

    # Toast notifications (optional)
    try:
        from winsdk.windows.ui.notifications import ToastNotificationManager  # noqa: F401

        print(f"  Toast notifications    : {_ok('available (winsdk)')}")
    except Exception:
        print(
            f"  Toast notifications    : "
            f"{_gray('not installed (optional). pip install iris-mcp[notifications]')}"
        )

    # Monitor topology (same source of truth screen_info uses: discovered live,
    # primary first, per-monitor DPI). Iris must work on any display layout.
    print()
    print(f"{BOLD}Monitors{RESET}")
    try:
        from iris import spatial as _spatial

        infos = _spatial.list_monitor_info(force_refresh=True)
        vd = _spatial.virtual_desktop()
        print(
            f"  {len(infos)} monitor(s); virtual desktop ({vd.x}, {vd.y}) -> "
            f"({vd.right}, {vd.bottom}) = {vd.width}x{vd.height}"
        )
        for m in infos:
            b = m.bounds
            tag = "PRIMARY" if m.primary else "       "
            print(
                f"  capture_id {m.capture_id}  {tag}  {m.device:<14} "
                f"({b.x:>6}, {b.y:>6}) -> ({b.right:>6}, {b.bottom:>6})  "
                f"size {b.width}x{b.height}  DPI {m.dpi}  scale {m.scale:.2f}x"
            )
    except Exception as e:
        print(f"  could not enumerate monitors: {e}")

    # Config locations
    print()
    print(f"{BOLD}Config locations{RESET}")
    print("  apps.yaml search order:")
    try:
        from iris.launcher import _apps_yaml_search_paths

        for p in _apps_yaml_search_paths():
            mark = _ok("EXISTS") if p.exists() else _gray("not present")
            print(f"    {p}  [{mark}]")
    except Exception as e:
        print(f"    (could not resolve: {e})")

    log_dir = os.environ.get("IRIS_LOG_DIR")
    if log_dir:
        print(f"  IRIS_LOG_DIR env       : {log_dir}")

    # Result
    print()
    if critical_ok:
        print(_ok(f"{BOLD}OK{RESET}  {_gray('all critical components available')}"))
        return 0
    print(_err(f"{BOLD}PROBLEMS{RESET}  {_gray('see warnings above')}"))
    return 1


if __name__ == "__main__":
    sys.exit(main())
