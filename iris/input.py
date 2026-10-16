"""SendInput-based mouse primitives.

The Win32 mouse_event API that pyautogui wraps is deprecated and can be filtered
or dropped by some apps (games, elevated windows, certain input hooks). SendInput
is the modern equivalent: atomic delivery, respects the active input desktop,
and survives focus-stealing protections.

Implementation notes:

* Coordinates are PHYSICAL screen pixels. The server sets
  PROCESS_PER_MONITOR_DPI_AWARE_V2 on startup so GetWindowRect, mss, and
  SendInput all agree on what "(x, y)" means. Without that flag this module
  would silently click in the wrong place on non-primary monitors.

* The absolute-coordinate calculation uses GetSystemMetrics(SM_XVIRTUALSCREEN,
  SM_YVIRTUALSCREEN, SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN) and maps to the
  0..65535 normalized space SendInput requires. The +1 in the denominator
  matches what Microsoft's example code does (avoids off-by-one at the right
  and bottom edges).

* This module deliberately does NOT depend on pyautogui or pygetwindow. We
  want a clean, minimal SendInput path so the click semantics are predictable.
"""

from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

try:
    _user32 = ctypes.WinDLL("user32", use_last_error=True)
    HAS_SENDINPUT = True
except (AttributeError, OSError):
    HAS_SENDINPUT = False


# ---------------------------------------------------------------------------
# Win32 structures
# ---------------------------------------------------------------------------
INPUT_MOUSE = 0
INPUT_KEYBOARD = 1

MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_RIGHTDOWN = 0x0008
MOUSEEVENTF_RIGHTUP = 0x0010
MOUSEEVENTF_MIDDLEDOWN = 0x0020
MOUSEEVENTF_MIDDLEUP = 0x0040
MOUSEEVENTF_WHEEL = 0x0800
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000

SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", wintypes.LONG),
        ("dy", wintypes.LONG),
        ("mouseData", wintypes.DWORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", wintypes.WORD),
        ("wScan", wintypes.WORD),
        ("dwFlags", wintypes.DWORD),
        ("time", wintypes.DWORD),
        ("dwExtraInfo", ctypes.c_void_p),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", wintypes.DWORD),
        ("wParamL", wintypes.WORD),
        ("wParamH", wintypes.WORD),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [
        ("type", wintypes.DWORD),
        ("u", _INPUT_UNION),
    ]


# ---------------------------------------------------------------------------
# Coordinate normalization
# ---------------------------------------------------------------------------
def _virtual_desktop() -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of the virtual desktop."""
    if not HAS_SENDINPUT:
        return (0, 0, 1, 1)
    gsm = _user32.GetSystemMetrics
    return (
        gsm(SM_XVIRTUALSCREEN),
        gsm(SM_YVIRTUALSCREEN),
        gsm(SM_CXVIRTUALSCREEN),
        gsm(SM_CYVIRTUALSCREEN),
    )


def _to_absolute(x: int, y: int) -> tuple[int, int]:
    """Map physical (x, y) into SendInput's 0..65535 virtual-desktop space.

    With MOUSEEVENTF_VIRTUALDESK set on the input flags, SendInput interprets
    dx/dy as (val * width / 65535) offsets from the virtual screen origin.
    """
    vl, vt, vw, vh = _virtual_desktop()
    # vw/vh are guaranteed > 0 on any active display config. Guard anyway.
    if vw <= 0 or vh <= 0:
        return 0, 0
    # round(), not int(): truncation biased every click up to 1 px toward the
    # top-left, which showed up as the bench's "1.0 px miss on the 4K monitor".
    nx = int(round(((x - vl) * 65535) / max(vw - 1, 1)))
    ny = int(round(((y - vt) * 65535) / max(vh - 1, 1)))
    return nx, ny


# ---------------------------------------------------------------------------
# Send helpers
# ---------------------------------------------------------------------------
SENDINPUT_CALLS = 0  # batches submitted; benches read this to prove the mouse was untouched


def sendinput_count() -> int:
    return SENDINPUT_CALLS


def _send(*events: _INPUT) -> int:
    """Submit one or more INPUT structs to SendInput. Returns events accepted."""
    global SENDINPUT_CALLS
    SENDINPUT_CALLS += 1
    if not HAS_SENDINPUT:
        return 0
    n = len(events)
    arr = (_INPUT * n)(*events)
    return _user32.SendInput(n, arr, ctypes.sizeof(_INPUT))


def _mouse_input(flags: int, x: int = 0, y: int = 0, data: int = 0) -> _INPUT:
    inp = _INPUT()
    inp.type = INPUT_MOUSE
    inp.mi = _MOUSEINPUT(
        dx=x,
        dy=y,
        mouseData=data,
        dwFlags=flags,
        time=0,
        dwExtraInfo=None,
    )
    return inp


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def position() -> tuple[int, int]:
    """Current cursor position in physical pixels."""
    if not HAS_SENDINPUT:
        return (0, 0)
    pt = wintypes.POINT()
    _user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def move(x: int, y: int) -> None:
    """Move the cursor to (x, y) in physical screen pixels. Instant, no easing."""
    if not HAS_SENDINPUT:
        return
    nx, ny = _to_absolute(int(x), int(y))
    _send(
        _mouse_input(
            MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
            x=nx,
            y=ny,
        )
    )


_BUTTON_FLAGS = {
    "left": (MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP),
    "right": (MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP),
    "middle": (MOUSEEVENTF_MIDDLEDOWN, MOUSEEVENTF_MIDDLEUP),
}


def click(
    x: int | None = None,
    y: int | None = None,
    button: str = "left",
    clicks: int = 1,
    double_click_gap_ms: int = 60,
    restore_cursor: bool = False,
) -> dict:
    """Click at (x, y) or at current position.

    Atomic: cursor move and button down/up are submitted in the same SendInput
    batch when coords are given, so no other input can slip between the move
    and the press. That's the reliability win over SetCursorPos + mouse_event.

    restore_cursor=True puts the pointer back where the user had it right
    after the click (the press itself still needs the real cursor, so the
    window gets focus; this only spares the user a pointer that wandered to
    another monitor).
    """
    if button not in _BUTTON_FLAGS:
        raise ValueError(f"button must be left/right/middle, got {button!r}")
    if not HAS_SENDINPUT:
        return {"ok": False, "reason": "sendinput_unavailable"}
    saved = position() if restore_cursor else None
    result = _click_impl(x, y, button, clicks, double_click_gap_ms)
    if saved is not None and result.get("ok"):
        move(*saved)
        result["cursor_restored_to"] = [saved[0], saved[1]]
    return result


# ---------------------------------------------------------------------------
# Quiet clicks: window messages instead of the shared cursor
#
# A posted WM_LBUTTONDOWN/UP pair reaches the target window through its own
# message queue: the pointer never moves, focus never changes, and the window
# may even be covered by another one. Most Win32, Tk, Qt, WPF and Chromium
# windows honor it. Apps that read the cursor position or raw input
# themselves (games, some DirectX overlays, a few custom toolkits) ignore it;
# the caller must verify and fall back to a SendInput click for those.
# ---------------------------------------------------------------------------
WM_MOUSEMOVE = 0x0200
WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK = 0x0201, 0x0202, 0x0203
WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK = 0x0204, 0x0205, 0x0206
WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK = 0x0207, 0x0208, 0x0209
MK_LBUTTON, MK_RBUTTON, MK_MBUTTON = 0x0001, 0x0002, 0x0010
CWP_SKIPINVISIBLE, CWP_SKIPDISABLED, CWP_SKIPTRANSPARENT = 0x0001, 0x0002, 0x0004

_MSG_BUTTONS = {
    "left": (WM_LBUTTONDOWN, WM_LBUTTONUP, WM_LBUTTONDBLCLK, MK_LBUTTON),
    "right": (WM_RBUTTONDOWN, WM_RBUTTONUP, WM_RBUTTONDBLCLK, MK_RBUTTON),
    "middle": (WM_MBUTTONDOWN, WM_MBUTTONUP, WM_MBUTTONDBLCLK, MK_MBUTTON),
}


def lparam_xy(x: int, y: int) -> int:
    """Pack client coords into a mouse-message LPARAM (low word x, high word y,
    two's complement for negatives like the real window manager does)."""
    return ((int(y) & 0xFFFF) << 16) | (int(x) & 0xFFFF)


def child_at_point(hwnd_top: int, x: int, y: int) -> int:
    """Deepest child window of `hwnd_top` under screen point (x, y), found
    by walking OUR window's children, so a window covering ours is ignored."""
    if not HAS_SENDINPUT:
        return int(hwnd_top)
    cur = int(hwnd_top)
    flags = CWP_SKIPINVISIBLE | CWP_SKIPDISABLED | CWP_SKIPTRANSPARENT
    for _ in range(32):
        pt = wintypes.POINT(int(x), int(y))
        if not _user32.ScreenToClient(cur, ctypes.byref(pt)):
            break
        child = _user32.ChildWindowFromPointEx(cur, pt, flags)
        if not child or child == cur:
            break
        cur = int(child)
    return cur


def message_click(
    hwnd_top: int,
    x: int,
    y: int,
    button: str = "left",
    clicks: int = 1,
) -> dict:
    """Deliver a click to the window under screen (x, y) inside `hwnd_top`
    by posting mouse messages. Never touches the cursor or the foreground.

    Returns {ok, method: 'message', target_hwnd, client: [cx, cy]}. ok only
    means the messages were queued; whether the app acted on them must be
    verified by the caller (receipt, fingerprint drift, verify_text).
    """
    if button not in _MSG_BUTTONS:
        raise ValueError(f"button must be left/right/middle, got {button!r}")
    if not HAS_SENDINPUT:
        return {"ok": False, "reason": "win32_unavailable", "method": "message"}
    down, up, dbl, mk = _MSG_BUTTONS[button]
    target = child_at_point(hwnd_top, x, y)
    pt = wintypes.POINT(int(x), int(y))
    _user32.ScreenToClient(target, ctypes.byref(pt))
    lp = lparam_xy(pt.x, pt.y)
    post = _user32.PostMessageW
    ok = bool(post(target, WM_MOUSEMOVE, 0, lp))
    n = max(1, int(clicks))
    for i in range(n):
        if i == 1:
            # Windows synthesizes DBLCLK for CS_DBLCLKS classes; post it too so
            # classes that only listen for the double-click message react.
            ok &= bool(post(target, dbl, mk, lp))
        else:
            ok &= bool(post(target, down, mk, lp))
        ok &= bool(post(target, up, 0, lp))
    return {
        "ok": ok,
        "method": "message",
        "target_hwnd": int(target),
        "client": [int(pt.x), int(pt.y)],
        "button": button,
        "clicks": n,
    }


def _click_impl(
    x: int | None,
    y: int | None,
    button: str,
    clicks: int,
    double_click_gap_ms: int,
) -> dict:
    down_flag, up_flag = _BUTTON_FLAGS[button]
    events: list[_INPUT] = []
    if x is not None and y is not None:
        nx, ny = _to_absolute(int(x), int(y))
        events.append(
            _mouse_input(
                MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK,
                x=nx,
                y=ny,
            )
        )
    # First click: emit move + down + up in one batch.
    first = list(events) + [_mouse_input(down_flag), _mouse_input(up_flag)]
    sent = _send(*first)
    # Subsequent clicks (double, triple) need a gap shorter than the system
    # double-click threshold (default 500ms) but long enough that the WM
    # registers them as separate. 60ms is the OBS / browser sweet spot.
    for _ in range(max(int(clicks) - 1, 0)):
        time.sleep(double_click_gap_ms / 1000.0)
        sent += _send(_mouse_input(down_flag), _mouse_input(up_flag))
    final_x, final_y = position()
    return {
        "ok": True,
        "x": final_x,
        "y": final_y,
        "events_sent": sent,
        "button": button,
        "clicks": int(clicks),
    }


def scroll(amount: int, x: int | None = None, y: int | None = None) -> dict:
    """Wheel scroll. Positive=up, negative=down. WHEEL_DELTA=120 per notch."""
    if not HAS_SENDINPUT:
        return {"ok": False, "reason": "sendinput_unavailable"}
    if x is not None and y is not None:
        move(int(x), int(y))
    delta = int(amount) * 120
    _send(_mouse_input(MOUSEEVENTF_WHEEL, data=delta))
    return {"ok": True, "amount": int(amount)}


# ---------------------------------------------------------------------------
# Drag engine: press, hold, move along a path, release. Used for drag and
# drop, sliders, faders and knobs (most knobs turn with a vertical drag).
# ---------------------------------------------------------------------------
KEYEVENTF_KEYUP = 0x0002
_MODIFIER_VK = {"shift": 0x10, "ctrl": 0x11, "control": 0x11, "alt": 0x12, "win": 0x5B}


def _key_input(vk: int, up: bool = False) -> _INPUT:
    inp = _INPUT()
    inp.type = INPUT_KEYBOARD
    inp.ki = _KEYBDINPUT(
        wVk=int(vk), wScan=0, dwFlags=KEYEVENTF_KEYUP if up else 0, time=0, dwExtraInfo=None
    )
    return inp


def modifier_vks(modifiers: list[str] | None) -> list[int]:
    out = []
    for m in modifiers or []:
        vk = _MODIFIER_VK.get(str(m).strip().lower())
        if vk is None:
            raise ValueError(f"unknown modifier {m!r}; use shift/ctrl/alt/win")
        out.append(vk)
    return out


def interpolate_path(
    points: list[tuple[int, int]], max_step_px: float = 6.0
) -> list[tuple[int, int]]:
    """Expand waypoints into small moves (<= max_step_px apart, rounded, no
    duplicates). Small steps matter: apps that measure a drag by its deltas
    (knobs, sliders) or detect a drag by movement past a threshold need to see
    continuous motion, not one jump."""
    if not points:
        return []
    out = [(int(round(points[0][0])), int(round(points[0][1])))]
    for (x0, y0), (x1, y1) in zip(points, points[1:], strict=False):
        dist = max(abs(x1 - x0), abs(y1 - y0))
        n = max(1, int(-(-dist // max_step_px)))
        for i in range(1, n + 1):
            p = (int(round(x0 + (x1 - x0) * i / n)), int(round(y0 + (y1 - y0) * i / n)))
            if p != out[-1]:
                out.append(p)
    return out


def drag_path(
    points: list[tuple[int, int]],
    *,
    button: str = "left",
    hold_ms: int = 80,
    step_ms: int = 8,
    release_ms: int = 60,
    modifiers: list[str] | None = None,
    restore_cursor: bool = True,
    max_step_px: float = 6.0,
) -> dict:
    """Press at points[0], hold, glide through the remaining waypoints, pause,
    release. Modifier keys are held for the whole gesture. The button and the
    modifiers are ALWAYS released (finally), so a failure mid-drag can never
    leave the user's mouse or Shift key stuck down."""
    if button not in _BUTTON_FLAGS:
        raise ValueError(f"button must be left/right/middle, got {button!r}")
    if not HAS_SENDINPUT:
        return {"ok": False, "reason": "sendinput_unavailable"}
    if len(points) < 2:
        raise ValueError("a drag needs at least a start and an end point")
    vks = modifier_vks(modifiers)
    down_flag, up_flag = _BUTTON_FLAGS[button]
    saved = position() if restore_cursor else None
    path = interpolate_path(points, max_step_px)
    pressed = False
    held: list[int] = []
    t0 = time.perf_counter()
    try:
        for vk in vks:
            _send(_key_input(vk))
            held.append(vk)
        move(*path[0])
        time.sleep(0.02)
        _send(_mouse_input(down_flag))
        pressed = True
        time.sleep(max(0, int(hold_ms)) / 1000.0)
        for p in path[1:]:
            move(*p)
            time.sleep(max(0, int(step_ms)) / 1000.0)
        time.sleep(max(0, int(release_ms)) / 1000.0)
    finally:
        if pressed:
            _send(_mouse_input(up_flag))
        for vk in reversed(held):
            _send(_key_input(vk, up=True))
        if saved is not None:
            move(*saved)
    return {
        "ok": True,
        "from": list(path[0]),
        "to": list(path[-1]),
        "moves": len(path) - 1,
        "ms": round((time.perf_counter() - t0) * 1000),
        "button": button,
        "modifiers": [str(m).lower() for m in modifiers or []],
        "cursor_restored_to": list(saved) if saved is not None else None,
    }


def scroll_at(
    x: int, y: int, notches: int, *, restore_cursor: bool = True, step_ms: int = 30
) -> dict:
    """Wheel `notches` (positive = up) with the pointer over (x, y), one notch
    at a time so apps that count wheel events (knobs, zoom) see each one."""
    if not HAS_SENDINPUT:
        return {"ok": False, "reason": "sendinput_unavailable"}
    saved = position() if restore_cursor else None
    try:
        move(int(x), int(y))
        n = int(notches)
        for _ in range(abs(n)):
            _send(_mouse_input(MOUSEEVENTF_WHEEL, data=120 if n > 0 else -120))
            time.sleep(max(0, int(step_ms)) / 1000.0)
    finally:
        if saved is not None:
            move(*saved)
    return {"ok": True, "notches": int(notches), "at": [int(x), int(y)]}


def drag(
    start_x: int,
    start_y: int,
    end_x: int,
    end_y: int,
    button: str = "left",
    duration_ms: int = 250,
    steps: int = 20,
) -> dict:
    """Legacy straight-line drag (mouse_drag tool). Delegates to drag_path so
    it also always releases the button; timing kept close to the old one."""
    steps = max(int(steps), 1)
    dist = max(abs(int(end_x) - int(start_x)), abs(int(end_y) - int(start_y)), 1)
    res = drag_path(
        [(int(start_x), int(start_y)), (int(end_x), int(end_y))],
        button=button,
        hold_ms=30,
        step_ms=max(1, int(duration_ms) // steps),
        release_ms=30,
        restore_cursor=False,
        max_step_px=max(1.0, dist / steps),
    )
    res.update({"steps": steps, "duration_ms": int(duration_ms)})
    return res
