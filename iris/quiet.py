"""Quiet-click policy: decide whether a posted mouse message will actually
click, and prove it when unsure.

Measured on 2026-09-06:
- Classic Win32 controls (Button in Character Map) act on posted
  WM_LBUTTONDOWN/UP: verified by the edit box receiving a character.
- Tk ignores them: Tk derives button state from GetKeyState(VK_LBUTTON),
  the PHYSICAL mouse, so a posted press generates no Tk event (52/53 quiet
  attempts on the Tk gauntlet produced no receipt; the one that did coincided
  with the user physically clicking elsewhere).

So the window class of the control under the point decides:
    trusted  -> post, done
    denied   -> do not post; caller uses the real mouse
    unknown  -> post, then watch the pixels around the point; if nothing
                repaints, report no_reaction so the caller falls back
Telemetry records class + outcome so the lists can grow from real use.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

from iris import input as input_mod
from iris import vision as vision_mod

try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    HAS_WIN32 = True
except (AttributeError, OSError):  # pragma: no cover
    HAS_WIN32 = False

# Classic Win32 control classes. Message-driven by construction.
TRUSTED_CLASSES = {
    "button",
    "edit",
    "static",
    "combobox",
    "combolbox",
    "listbox",
    "scrollbar",
    "syslistview32",
    "systreeview32",
    "systabcontrol32",
    "sysheader32",
    "toolbarwindow32",
    "msctls_trackbar32",
    "msctls_updown32",
    "msctls_progress32",
    "rebarwindow32",
    "richedit20w",
    "richedit50w",
    "#32770",  # dialog
    "#32768",  # menu popup
}
# Toolkits that read the physical mouse instead of the message.
DENIED_CLASSES = {
    "tktoplevel",
    "tkchild",
    "windows.ui.core.corewindow",
    "applicationframewindow",
    "applicationframeinputsinkwindow",
    "sunawtframe",
    "sunawtcanvas",
    "sunawtdialog",
    "sunawtwindow",
    "unitywndclass",
    "unrealwindow",
}
DENIED_PREFIXES = ("sunawt", "tk")

REACTION_ROI = 90  # px around the click point that must repaint
REACTION_WAITS_MS = (35, 45, 60, 100, 160)  # cumulative ~400 ms; returns at the first repaint


def window_class(hwnd: int) -> str:
    if not HAS_WIN32 or not hwnd:
        return ""
    buf = ctypes.create_unicode_buffer(256)
    try:
        n = _user32.GetClassNameW(int(hwnd), buf, 256)
        return buf.value[:n] if n else ""
    except Exception:
        return ""


def classify_class(cls: str) -> str:
    c = (cls or "").strip().lower()
    if not c:
        return "unknown"
    if c in DENIED_CLASSES or any(c.startswith(p) for p in DENIED_PREFIXES):
        return "denied"
    if c in TRUSTED_CLASSES:
        return "trusted"
    return "unknown"


def classify_target(hwnd_top: int, x: int, y: int) -> dict:
    """Which child window will receive a posted click at screen (x, y), its
    class, and the verdict. Walks UP from the leaf too: a leaf 'Static'
    inside a Tk pane is still Tk."""
    target = input_mod.child_at_point(hwnd_top, x, y)
    cls = window_class(target)
    verdict = classify_class(cls)
    # A trusted/unknown leaf inside a denied top-level (Tk draws its widgets
    # in TkChild panes) must inherit the denial.
    top_cls = window_class(hwnd_top)
    if classify_class(top_cls) == "denied":
        verdict = "denied"
    return {"target_hwnd": int(target), "class": cls, "top_class": top_cls, "verdict": verdict}


def _roi_digest(hwnd_top: int, x: int, y: int, r: int = REACTION_ROI) -> str | None:
    try:
        img = vision_mod.capture_window(int(hwnd_top))
        rect = wintypes.RECT()
        _user32.GetWindowRect(int(hwnd_top), ctypes.byref(rect))
        lx, ly = int(x) - rect.left, int(y) - rect.top
        box = (max(0, lx - r), max(0, ly - r), min(img.width, lx + r), min(img.height, ly + r))
        if box[2] <= box[0] or box[3] <= box[1]:
            return None
        return vision_mod.exact_digest(img.crop(box))
    except Exception:
        return None


def message_click_checked(
    hwnd_top: int,
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
    *,
    force: bool = False,
) -> dict:
    """Post a click if the target's class allows it, proving it for unknown
    classes. Returns the input.message_click dict plus `verdict`, `class`,
    `reaction`. ok=False with reason 'class_denied' or 'no_reaction' means
    the caller should use the real mouse instead."""
    info = classify_target(hwnd_top, x, y)
    if info["verdict"] == "denied" and not force:
        return {"ok": False, "reason": "class_denied", "method": "message", **info}
    before = None
    if info["verdict"] == "unknown" and not force:
        before = _roi_digest(hwnd_top, x, y)
    res = input_mod.message_click(hwnd_top, x, y, button=button, clicks=clicks)
    res.update(info)
    if not res.get("ok"):
        res.setdefault("reason", "post_failed")
        return res
    if before is None:
        res["reaction"] = None  # trusted class (or unverifiable): not checked
        return res
    for wait_ms in REACTION_WAITS_MS:
        time.sleep(wait_ms / 1000.0)
        after = _roi_digest(hwnd_top, x, y)
        if after is not None and after != before:
            res["reaction"] = True
            return res
    res["ok"] = False
    res["reason"] = "no_reaction"
    res["reaction"] = False
    return res
